"""Reading the on-board profiles of keyboards with Profile Management (0x8101), e.g. the G915 X.

Learned from a USB recording of G HUB (tools/ghub-capture). Read-only on purpose: only the
two calls G HUB uses to read are implemented.

* fn 8 opens a file: ``bank, file, size_hi, size_lo`` (bank 0 holds the profiles in use,
  bank 1 the factory ones; file 0 is the bank's directory, 0x400 bytes).
* fn 12 reads 16 bytes of the open file: ``offset_hi, offset_lo, 0``.

Directory: ``crc32(4) count(1)`` then entries ``profile(1) 81 01 40 63 crc32(4) file(1)``, ended by 0xFF.

Profile file (0x63 bytes)::

    0      index (0 in bank 0)        1  version?
    2..3   power-save timeout (s)     4..5  power-off timeout (s)
    6..29  name, 12 UTF-16BE characters (G HUB shortens long names with "…")
    30..51 lighting at full power: effect id, then 10 effect parameters
    52..73 lighting in power-save mode, same layout
    74..   other settings (not decoded)

The custom animations that effect 0x0C plays live in separate storage that G HUB never
reads back (it keeps its own copy), so their per-key colours can't be shown; their names can
(features.lighting.RGBEffects.onboard_effect_names).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..hidpp import Device, HIDPPError
from ..hidpp import features as F

PROFILE_SIZE = 0x63
DIR_SIZE = 0x400

# 0x8071 effect ids as they appear in profiles
EFFECT_NAMES = {
    0x00: "Off", 0x01: "Static", 0x0A: "Breathing", 0x0B: "Ripple", 0x0C: "Custom animation",
    0x13: "Effect 0x13", 0x15: "Colour cycle", 0x16: "Colour wave", 0x17: "Ripple",
    0x18: "Two-colour effect", 0x19: "Two-colour effect (alternate)",
}


@dataclass
class StoredEffect:
    effect_id: int
    params: bytes

    @property
    def name(self) -> str:
        return EFFECT_NAMES.get(self.effect_id, f"Effect {self.effect_id:#04x}")

    @property
    def colors(self) -> list[int]:
        p = self.params
        if self.effect_id in (0x01, 0x0A, 0x0B, 0x17):
            return [int.from_bytes(p[0:3], "big")]
        if self.effect_id in (0x18, 0x19):  # colour 1, colour 2, period, brightness (as G HUB sends it)
            return [int.from_bytes(p[0:3], "big"), int.from_bytes(p[3:6], "big")]
        return []

    @property
    def brightness(self) -> int | None:
        if self.effect_id == 0x0C:
            return self.params[7]
        if self.effect_id in (0x18, 0x19):
            return self.params[8]
        return None

    def describe(self) -> str:
        parts = [self.name]
        if self.colors:
            parts.append(" → ".join(f"#{c:06x}" for c in self.colors))
        if self.brightness is not None:
            parts.append(f"{self.brightness}%")
        return ", ".join(parts)

    def to_lighting(self) -> dict | None:
        """The closest profile lighting dict, for the preview (None when it can't be drawn)."""
        c = self.colors
        if self.effect_id == 0x01 and c:
            return {"zones": {"0": {"effect": 0x01, "color": c[0]}, "1": {"effect": 0x01, "color": c[0]}}}
        if self.effect_id in (0x18, 0x19) and len(c) == 2:
            return {"animation": {"enabled": True, "colors": c, "step_ms": 1500, "fade": True, "pattern": "all",
                                  "keep_painted": False}}
        if self.effect_id in (0x15, 0x16):
            return {"zones": {"0": {"effect": self.effect_id}, "1": {"effect": 0x15}}}
        return None


@dataclass
class StoredProfile:
    number: int  # 1-3, as the keyboard's profile keys count them
    file: int
    name: str
    powersave_timeout: int
    poweroff_timeout: int
    lighting: StoredEffect
    powersave_lighting: StoredEffect
    raw: bytes


def parse_directory(data: bytes) -> list[tuple[int, int, int]]:
    """[(profile number, file id, crc32)]"""
    out = []
    count = data[4]
    i = 5
    for _ in range(count):
        if i + 10 > len(data) or data[i] == 0xFF:
            break
        number = data[i]
        crc = struct.unpack(">I", data[i + 5 : i + 9])[0]
        out.append((number, data[i + 9], crc))
        i += 10
    return out


def parse_profile(number: int, file: int, data: bytes) -> StoredProfile:
    name = data[6:30].decode("utf-16-be", errors="replace").split("\x00")[0]
    if set(data[6:30]) == {0xFF}:
        name = ""
    return StoredProfile(
        number=number, file=file, name=name,
        powersave_timeout=struct.unpack(">H", data[2:4])[0],
        poweroff_timeout=struct.unpack(">H", data[4:6])[0],
        lighting=StoredEffect(data[30], bytes(data[31:41])),
        powersave_lighting=StoredEffect(data[52], bytes(data[53:63])),
        raw=bytes(data),
    )


class ProfileFS:
    def __init__(self, dev: Device):
        self.dev = dev

    def read_file(self, bank: int, file: int, size: int) -> bytes:
        self.dev.call(F.PROFILE_MANAGEMENT, 8, bank, file, size >> 8, size & 0xFF)
        out = b""
        for off in range(0, size, 16):
            out += self.dev.call(F.PROFILE_MANAGEMENT, 12, off >> 8, off & 0xFF, 0)
        return out[:size]

    def active(self) -> tuple[bool, int]:
        """(in on-board mode, number of the profile in use), from the state query G HUB sends (fn 6, zeros).

        The reply's first byte has bit 0 set in on-board mode and is 0 while the host owns the keyboard.
        """
        d = self.dev.call(F.PROFILE_MANAGEMENT, 6, 0, 0, 0)
        return bool(d[0] & 1), d[2]  # bit 1 is set too at times (seen after animation uploads)

    def select(self, number: int) -> None:
        """Make on-board profile ``number`` the one in use, as G HUB does (fn 9 naming the profile and its CRC).

        The keyboard's Fn + F2/F3/F4 do the same for profiles 1-3.
        """
        for n, _file, crc in parse_directory(self.read_file(0, 0, DIR_SIZE)):
            if n == number:
                self.dev.call(F.PROFILE_MANAGEMENT, 9, 0x81, 0x01, 0x01, number >> 8, number & 0xFF,
                              PROFILE_SIZE >> 8, PROFILE_SIZE & 0xFF, crc.to_bytes(4, "big"))
                return
        raise ValueError(f"there is no on-board profile {number}")

    def profiles(self, bank: int = 0) -> list[StoredProfile]:
        entries = parse_directory(self.read_file(bank, 0, DIR_SIZE))
        return [parse_profile(n, f, self.read_file(bank, f, PROFILE_SIZE)) for n, f, _crc in entries]
