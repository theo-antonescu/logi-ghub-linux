"""Lighting: 0x8071 RGB effects, 0x8070 colour LED effects, 0x8081 per-key, 0x8040 brightness.

Byte layouts follow Solaar's implementation (which is based on packet
captures of G HUB), and are confirmed against the devices probed in
data/devices/.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

from ..hidpp import Device, HIDPPError
from ..hidpp import features as F

log = logging.getLogger(__name__)

LOCATIONS = {
    0x00: "Unknown", 0x01: "Primary", 0x02: "Logo", 0x03: "Left side", 0x04: "Right side", 0x05: "Combined",
    0x06: "Primary 1", 0x07: "Primary 2", 0x08: "Primary 3", 0x09: "Primary 4", 0x0A: "Primary 5", 0x0B: "Primary 6",
}

# effect id -> (name, {param: offset}) ; offsets are into the 10 parameter bytes
EFFECTS: dict[int, tuple[str, dict[str, int]]] = {
    0x00: ("Off", {}),
    0x01: ("Static", {"color": 0, "ramp": 3}),
    0x02: ("Pulse", {"color": 0, "speed": 3}),
    0x03: ("Color cycle", {"period": 5, "intensity": 7}),
    0x04: ("Color wave", {"period": 6, "direction": 9}),
    0x08: ("Boot animation", {}),
    0x09: ("Demo", {}),
    0x0A: ("Breathing", {"color": 0, "period": 3, "form": 5, "intensity": 6}),
    0x0B: ("Ripple", {"color": 0, "period": 4}),
    0x0E: ("Decomposition", {"period": 6, "intensity": 8}),
    0x0F: ("Signature 1", {"period": 5, "intensity": 7}),
    0x10: ("Signature 2", {"period": 5, "intensity": 7}),
    0x15: ("Color cycle", {"saturation": 1, "period": 6, "intensity": 8}),
    0x16: ("Color wave", {"saturation": 1, "period": 6, "intensity": 8, "direction": 9}),
    0x17: ("Ripple", {"color": 0, "saturation": 3, "period": 6}),
}
PARAM_SIZES = {"color": 3, "speed": 1, "period": 2, "intensity": 1, "ramp": 1, "form": 1, "saturation": 1, "direction": 1}
DIRECTIONS = {0: "Cycle", 1: "Right", 2: "Down", 3: "Center out", 4: "In", 5: "Out", 6: "Left", 7: "Up", 8: "Center in"}
DEFAULTS = {"period": 5000, "intensity": 100, "saturation": 255, "speed": 10, "ramp": 0, "form": 0, "direction": 1}


@dataclass
class EffectInfo:
    index: int
    effect_id: int
    capabilities: int
    period: int

    @property
    def name(self) -> str:
        return EFFECTS.get(self.effect_id, (f"Effect {self.effect_id:#04x}", {}))[0]

    @property
    def params(self) -> list[str]:
        return list(EFFECTS.get(self.effect_id, ("", {}))[1])


@dataclass
class Zone:
    index: int
    location: int
    effects: list[EffectInfo] = field(default_factory=list)

    @property
    def name(self) -> str:
        return LOCATIONS.get(self.location, f"Zone {self.index}")

    def effect_by_id(self, effect_id: int) -> EffectInfo | None:
        return next((e for e in self.effects if e.effect_id == effect_id), None)


@dataclass
class Effect:
    """A concrete effect choice: an effect id plus parameter values."""

    effect_id: int
    color: int = 0xFFFFFF
    period: int = 5000
    intensity: int = 100
    saturation: int = 255
    speed: int = 10
    direction: int = 1
    ramp: int = 0
    form: int = 0

    def params_bytes(self) -> bytes:
        b = bytearray(10)
        for p, off in EFFECTS.get(self.effect_id, ("", {}))[1].items():
            size = PARAM_SIZES[p]
            b[off : off + size] = int(getattr(self, p)).to_bytes(size, "big")
        return bytes(b)

    def to_dict(self) -> dict:
        d = {"effect": self.effect_id}
        for p in EFFECTS.get(self.effect_id, ("", {}))[1]:
            d[p] = getattr(self, p)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Effect":
        e = cls(int(d["effect"]))
        for k, v in d.items():
            if k != "effect" and hasattr(e, k):
                setattr(e, k, int(v, 16) if isinstance(v, str) and k == "color" else int(v))
        return e

    @property
    def name(self) -> str:
        return EFFECTS.get(self.effect_id, (f"Effect {self.effect_id:#04x}", {}))[0]


class RGBEffects:
    """0x8071. The host must claim software control before its effects stick."""

    FLAG_NVCONFIG = 0x04

    def __init__(self, dev: Device):
        self.dev = dev
        info = dev.call(F.RGB_EFFECTS, 0x0, 0xFF, 0xFF, 0x00)
        self.zone_count = info[2]
        self.nv_caps, self.ext_caps = struct.unpack(">HH", info[3:7])
        self.zones: list[Zone] = []
        for z in range(self.zone_count):
            zi = dev.call(F.RGB_EFFECTS, 0x0, z, 0xFF, 0x00)
            location = struct.unpack(">H", zi[2:4])[0]
            count = zi[4]
            zone = Zone(z, location)
            for e in range(count):
                ei = dev.call(F.RGB_EFFECTS, 0x0, z, e, 0x00)
                eid, caps, period = struct.unpack(">HHH", ei[2:8])
                zone.effects.append(EffectInfo(e, eid, caps, period))
            self.zones.append(zone)

    def get_sw_control(self) -> tuple[int, int]:
        d = self.dev.call(F.RGB_EFFECTS, 0x5, 0x00)
        return d[1], d[2]

    FLAG_POWER = 0x02

    def claim(self) -> None:
        """Take lighting over from the firmware (what G HUB does while running).

        The device first has to leave its on-board profile: newer keyboards do
        that through Profile Management (0x8101), mice through 0x8100 host mode.
        """
        if self.dev.has(F.PROFILE_MANAGEMENT):
            self.dev.call(F.PROFILE_MANAGEMENT, 0x6, 0x05)
        elif self.dev.has(F.ONBOARD_PROFILES):
            self.dev.call(F.ONBOARD_PROFILES, 0x1, 0x02)
        # FLAG_POWER hands idle handling to the host too. Without it the firmware's own power-save
        # brings the stored on-board animation back over the host's colours after a minute.
        self.dev.call(F.RGB_EFFECTS, 0x5, 0x01, 0x03, self.FLAG_NVCONFIG | self.FLAG_POWER)

    def release(self) -> None:
        self.dev.call(F.RGB_EFFECTS, 0x5, 0x01, 0x00, 0x00)
        if self.dev.has(F.PROFILE_MANAGEMENT):
            self.dev.call(F.PROFILE_MANAGEMENT, 0x6, 0x03)
        elif self.dev.has(F.ONBOARD_PROFILES):
            self.dev.call(F.ONBOARD_PROFILES, 0x1, 0x01)

    def onboard_effect_names(self) -> list[str]:
        """Names of the effects G HUB stored in the device (getInfo type 0x01, name chunks 4-6)."""
        names = []
        for slot in range(8):
            try:
                parts = [self.dev.call(F.RGB_EFFECTS, 0x0, 0xFF, 0x00, 0x01, slot, kind, bytes(11))[5:16] for kind in (4, 5, 6)]
            except HIDPPError:
                break
            name = b"".join(parts).split(b"\0")[0].decode("utf-8", "replace").strip()
            names.append(name or f"Effect {slot + 1}")
        return names

    # Power handling while the host owns the lighting (claimed with FLAG_POWER):
    # the firmware reports user activity (function 1 notifications, data[0] == 0
    # means idle) and the host must switch the LEDs off and back on itself.
    def power(self, on: bool) -> None:
        self.dev.call(F.RGB_EFFECTS, 0x8, 0x01, 0x01 if on else 0x03, 0x00)

    def activity(self, msg) -> bool | None:
        """For an onUserActivity notification return True (active) / False (idle), else None."""
        if msg.sub_id != self.dev.features.get(F.RGB_EFFECTS) or msg.function != 1 or msg.swid != 0:
            return None
        return bool(msg.data[0])

    def set_effect(self, zone: int, effect: Effect, persist: bool = True) -> None:
        info = self.zones[zone].effect_by_id(effect.effect_id)
        if info is None:
            raise ValueError(f"zone {zone} does not support {effect.name}")
        self.dev.call(F.RGB_EFFECTS, 0x1, zone, info.index, effect.params_bytes(), 0x01 if persist else 0x00)


class ColorLedEffects:
    """0x8070, the older zone effect feature."""

    def __init__(self, dev: Device):
        self.dev = dev
        info = dev.call(F.COLOR_LED_EFFECTS, 0x0)
        self.zone_count = info[0]
        self.zones: list[Zone] = []
        for z in range(self.zone_count):
            zi = dev.call(F.COLOR_LED_EFFECTS, 0x1, z)
            location = struct.unpack(">H", zi[1:3])[0]
            zone = Zone(z, location)
            for e in range(zi[3]):
                ei = dev.call(F.COLOR_LED_EFFECTS, 0x2, z, e)
                eid, caps, period = struct.unpack(">HHH", ei[2:8])
                zone.effects.append(EffectInfo(e, eid, caps, period))
            self.zones.append(zone)

    def claim(self) -> None:
        self.dev.call(F.COLOR_LED_EFFECTS, 0x8, 0x01)

    def release(self) -> None:
        self.dev.call(F.COLOR_LED_EFFECTS, 0x8, 0x00)

    def set_effect(self, zone: int, effect: Effect, persist: bool = True) -> None:
        info = self.zones[zone].effect_by_id(effect.effect_id)
        if info is None:
            raise ValueError(f"zone {zone} does not support {effect.name}")
        self.dev.call(F.COLOR_LED_EFFECTS, 0x3, zone, info.index, effect.params_bytes(), 0x01 if persist else 0x00)


class PerKeyLighting:
    """0x8081: per-key colours. Zone ids are keyboard positions (A=1, ..., see KEY_ZONES)."""

    def __init__(self, dev: Device):
        self.dev = dev
        bitmap = b""
        for page in range(3):
            bitmap += dev.call(F.PER_KEY_LIGHTING_V2, 0x0, 0x00, page)[2:16]
        self.zones = [i for i in range(1, min(255, len(bitmap) * 8)) if bitmap[i // 8] >> (i % 8) & 1]

    def set_colors(self, colors: dict[int, int]) -> None:
        """Set ``{zone: 0xRRGGBB}`` and commit the frame."""
        by_color: dict[int, list[int]] = {}
        for z, c in colors.items():
            by_color.setdefault(c, []).append(z)
        singles = b""
        for c, zones in by_color.items():
            rgb = c.to_bytes(3, "big")
            zones = sorted(zones)
            while len(zones) > 3:  # one colour for up to 13 keys per report
                self.dev.call(F.PER_KEY_LIGHTING_V2, 0x6, rgb, bytes(zones[:13]))
                zones = zones[13:]
            for z in zones:
                singles += bytes([z]) + rgb
                if len(singles) == 16:
                    self.dev.call(F.PER_KEY_LIGHTING_V2, 0x1, singles)
                    singles = b""
        if singles:
            self.dev.call(F.PER_KEY_LIGHTING_V2, 0x1, singles)
        self.frame_end()

    def fill(self, color: int) -> None:
        zones = self.zones
        if zones:
            self.dev.call(F.PER_KEY_LIGHTING_V2, 0x5, zones[0], zones[-1], color.to_bytes(3, "big"))
        self.frame_end()

    def frame_end(self) -> None:
        self.dev.call(F.PER_KEY_LIGHTING_V2, 0x7, 0x00)


class Brightness:
    """0x8040 global brightness."""

    def __init__(self, dev: Device):
        self.dev = dev
        d = dev.call(F.BRIGHTNESS_CONTROL, 0x0)
        self.max = struct.unpack(">H", d[0:2])[0]
        self.steps = d[2] & 0x0F
        self.caps = d[3]
        self.min = struct.unpack(">H", d[4:6])[0]

    def get(self) -> int:
        return struct.unpack(">H", self.dev.call(F.BRIGHTNESS_CONTROL, 0x1)[0:2])[0]

    def set(self, value: int) -> None:
        value = max(self.min, min(self.max, value))
        self.dev.call(F.BRIGHTNESS_CONTROL, 0x2, value >> 8, value & 0xFF)


# Per-key zone ids follow HID keyboard usages shifted down by 3 for the main
# block (A = usage 0x04 = zone 1) and by 120 for modifiers (LeftCtrl 0xE0 = 104).
SPECIAL_ZONES = {"Brightness": 153, "Play/Pause": 155, "Mute": 156, "Next": 157, "Previous": 158,
                 "G1": 180, "G2": 181, "G3": 182, "G4": 183, "G5": 184, "Logo": 210}


def zone_for_hid(usage: int) -> int | None:
    if 0x04 <= usage <= 0x65:
        return usage - 3
    if 0xE0 <= usage <= 0xE7:
        return usage - 120
    return None


def hid_for_zone(zone: int) -> int | None:
    if 1 <= zone <= 0x62:
        return zone + 3
    if 104 <= zone <= 111:
        return zone + 120
    return None
