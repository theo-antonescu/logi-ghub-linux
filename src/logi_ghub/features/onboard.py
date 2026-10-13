"""HID++ 0x8100 Onboard Profiles: the device's own memory.

Memory is organised in sectors of ``sector_size`` bytes (255 on the G502 X,
256 on older mice). Every sector ends with a big-endian CRC-16/CCITT of the
preceding bytes.

* sector 0x0000 is the profile directory: 4-byte entries
  ``sector_hi sector_lo enabled 00`` ended by ``ff ff``.
* sectors 0x0001.. hold user profiles, 0x0100+n hold read-only factory
  profiles.
* remaining writable sectors are free for macros.

Profile layout (formats 1-5, as documented by libratbag and Solaar and checked
against a G502 X dump)::

    0      report rate (ms)
    1      default DPI index
    2      DPI-shift index
    3..12  five DPI values, little endian
    13..15 profile colour
    16     power mode           17 angle snapping
    18..19 write counter (G HUB)  20..27 reserved
    28..29 power-save timeout     30..31 power-off timeout
    32..95   16 button bindings
    96..159  16 G-Shift bindings
    160..207 name, UTF-16LE, 24 characters
    208..251 four 11-byte lighting effects
    ...      unused, then CRC
"""

from __future__ import annotations

import json
import logging
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..actions import Action
from ..hidpp import Device, HIDPPError
from ..hidpp import features as F

log = logging.getLogger(__name__)

MODE_NO_CHANGE = 0
MODE_ONBOARD = 1
MODE_HOST = 2

ROM_BASE = 0x0100
DIRECTORY = 0x0000


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF), as used for sectors."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def sector_valid(data: bytes) -> bool:
    return len(data) > 2 and crc16(data[:-2]) == struct.unpack(">H", data[-2:])[0]


def seal(data: bytes | bytearray) -> bytes:
    """Return ``data`` with its trailing CRC recomputed."""
    body = bytes(data[:-2])
    return body + struct.pack(">H", crc16(body))


@dataclass
class OnboardInfo:
    memory_model: int
    profile_format: int
    macro_format: int
    profile_count: int
    rom_profile_count: int
    button_count: int
    sector_count: int
    sector_size: int
    mechanical_layout: int
    various_info: int

    @classmethod
    def parse(cls, d: bytes) -> "OnboardInfo":
        return cls(d[0], d[1], d[2], d[3], d[4], d[5], d[6], struct.unpack(">H", d[7:9])[0], d[9], d[10])

    @property
    def has_gshift(self) -> bool:
        return self.mechanical_layout & 0x03 == 0x02

    @property
    def has_dpi_shift(self) -> bool:
        return (self.mechanical_layout >> 2) & 0x03 == 0x02

    @property
    def supported(self) -> bool:
        return self.memory_model == 1 and 1 <= self.profile_format <= 5 and self.macro_format == 1


@dataclass
class LightingEffect:
    """An 11-byte on-board lighting slot. ``raw`` is always kept so unknown modes round-trip."""

    raw: bytes

    MODES = {0x00: "Off", 0x01: "Static", 0x03: "Cycle", 0x04: "Wave", 0x05: "Starlight", 0x0A: "Breathing", 0x0B: "Ripple"}

    @property
    def mode(self) -> int:
        return self.raw[0]

    @property
    def color(self) -> tuple[int, int, int]:
        return tuple(self.raw[1:4])

    @property
    def period(self) -> int:
        if self.mode == 0x03:
            return struct.unpack(">H", self.raw[6:8])[0]
        if self.mode in (0x0A, 0x0B):
            return struct.unpack(">H", self.raw[4:6])[0]
        return 0

    @property
    def brightness(self) -> int:
        b = self.raw[8] if self.mode == 0x03 else self.raw[7] if self.mode == 0x0A else 100
        return b or 100

    @classmethod
    def make(cls, mode: int, color=(0, 0, 0), period: int = 0, brightness: int = 100) -> "LightingEffect":
        b = bytearray(11)
        b[0] = mode
        r, g, bl = color
        intensity = 0 if brightness >= 100 else brightness
        if mode == 0x01:
            b[1:4] = bytes([r, g, bl])
        elif mode == 0x03:
            b[6:8] = struct.pack(">H", period or 5000)
            b[8] = intensity
        elif mode == 0x0A:
            b[1:4] = bytes([r, g, bl])
            b[4:6] = struct.pack(">H", period or 3000)
            b[7] = intensity
        elif mode == 0x0B:
            b[1:4] = bytes([r, g, bl])
            b[5:7] = struct.pack(">H", period or 20)
        return cls(bytes(b))

    def describe(self) -> str:
        name = self.MODES.get(self.mode, f"mode {self.mode:#04x}")
        if self.mode in (0x01, 0x0A, 0x0B):
            name += " #%02x%02x%02x" % self.color
        return name


class OnboardProfile:
    """A profile sector, kept as raw bytes with typed accessors so unknown bytes survive a rewrite."""

    def __init__(self, data: bytes, sector: int = 0, enabled: bool = True):
        self.raw = bytearray(data)
        self.sector = sector
        self.enabled = enabled

    # scalar fields
    def _u8(self, off: int) -> int:
        return self.raw[off]

    @property
    def report_rate_ms(self) -> int:
        return self.raw[0]

    @report_rate_ms.setter
    def report_rate_ms(self, v: int) -> None:
        self.raw[0] = v

    @property
    def default_dpi_index(self) -> int:
        return self.raw[1]

    @default_dpi_index.setter
    def default_dpi_index(self, v: int) -> None:
        self.raw[1] = v

    @property
    def shift_dpi_index(self) -> int:
        return self.raw[2]

    @shift_dpi_index.setter
    def shift_dpi_index(self, v: int) -> None:
        self.raw[2] = v

    @property
    def dpis(self) -> list[int]:
        """DPI stages; 0 or 0xFFFF marks an unused stage."""
        return [struct.unpack("<H", self.raw[3 + 2 * i : 5 + 2 * i])[0] for i in range(5)]

    @dpis.setter
    def dpis(self, values: list[int]) -> None:
        values = list(values)[:5]
        values += [0] * (5 - len(values))
        for i, v in enumerate(values):
            self.raw[3 + 2 * i : 5 + 2 * i] = struct.pack("<H", v)

    @property
    def active_dpis(self) -> list[int]:
        return [d for d in self.dpis if d not in (0, 0xFFFF)]

    @property
    def color(self) -> tuple[int, int, int]:
        return tuple(self.raw[13:16])

    @property
    def angle_snapping(self) -> int:
        return self.raw[17]

    @property
    def powersave_timeout(self) -> int:
        return struct.unpack("<H", self.raw[28:30])[0]

    @property
    def poweroff_timeout(self) -> int:
        return struct.unpack("<H", self.raw[30:32])[0]

    # bindings
    def button(self, i: int, gshift: bool = False) -> Action:
        off = (96 if gshift else 32) + 4 * i
        return Action.from_bytes(self.raw[off : off + 4])

    def set_button(self, i: int, action: Action, gshift: bool = False) -> None:
        off = (96 if gshift else 32) + 4 * i
        self.raw[off : off + 4] = action.to_bytes()

    def buttons(self, count: int = 16, gshift: bool = False) -> list[Action]:
        return [self.button(i, gshift) for i in range(count)]

    # name
    @property
    def name(self) -> str:
        raw = bytes(self.raw[160:208])
        if raw == b"\xff" * 48:
            return ""
        return raw.decode("utf-16le", errors="replace").split("\x00")[0].rstrip("￿")

    @name.setter
    def name(self, value: str) -> None:
        if not value:
            self.raw[160:208] = b"\xff" * 48
        else:
            self.raw[160:208] = value[:24].ljust(24, "\x00").encode("utf-16le")

    # lighting
    def lighting(self, i: int) -> LightingEffect:
        return LightingEffect(bytes(self.raw[208 + 11 * i : 219 + 11 * i]))

    def set_lighting(self, i: int, effect: LightingEffect) -> None:
        self.raw[208 + 11 * i : 219 + 11 * i] = effect.raw

    def to_bytes(self) -> bytes:
        return seal(self.raw)

    def copy(self) -> "OnboardProfile":
        return OnboardProfile(bytes(self.raw), self.sector, self.enabled)


# --- macros ---------------------------------------------------------------
# On-board macro opcodes (macro format 1)
OP_NOOP = 0x00
OP_WAIT_RELEASE = 0x01
OP_REPEAT_WHILE_PRESSED = 0x02
OP_REPEAT_UNTIL_CANCEL = 0x03
OP_ROLLER = 0x20
OP_ACPAN = 0x21
OP_DELAY = 0x40
OP_BUTTON_DOWN = 0x41
OP_BUTTON_UP = 0x42
OP_KEY_DOWN = 0x43
OP_KEY_UP = 0x44
OP_CONSUMER_DOWN = 0x45
OP_CONSUMER_UP = 0x46
OP_JUMP = 0x60
OP_XY = 0x61
OP_END = 0xFF

OP_SIZES = {
    OP_NOOP: 1, OP_WAIT_RELEASE: 1, OP_REPEAT_WHILE_PRESSED: 1, OP_REPEAT_UNTIL_CANCEL: 1, OP_END: 1,
    OP_ROLLER: 2, OP_ACPAN: 2,
    OP_DELAY: 3, OP_BUTTON_DOWN: 3, OP_BUTTON_UP: 3, OP_KEY_DOWN: 3, OP_KEY_UP: 3,
    OP_CONSUMER_DOWN: 3, OP_CONSUMER_UP: 3, OP_JUMP: 3,
    OP_XY: 5,
}


def parse_macro_bytes(memory: bytes, offset: int = 0) -> list[tuple[int, bytes]]:
    """Decode macro opcodes from one sector starting at ``offset`` until END."""
    ops = []
    i = offset
    while i < len(memory):
        op = memory[i]
        size = OP_SIZES.get(op)
        if size is None:
            raise ValueError(f"unknown macro opcode {op:#04x} at offset {i}")
        ops.append((op, bytes(memory[i + 1 : i + size])))
        if op in (OP_END, OP_JUMP):
            break
        i += size
    return ops


def encode_macro_ops(ops: list[tuple[int, bytes]]) -> bytes:
    out = bytearray()
    for op, args in ops:
        if OP_SIZES.get(op, -1) != 1 + len(args):
            raise ValueError(f"bad arguments for macro opcode {op:#04x}")
        out.append(op)
        out += args
    if not ops or ops[-1][0] != OP_END:
        out.append(OP_END)
    return bytes(out)


# --- the feature ----------------------------------------------------------
class OnboardProfiles:
    def __init__(self, device: Device):
        self.dev = device
        self.info = OnboardInfo.parse(device.call(F.ONBOARD_PROFILES, 0x0))

    # mode / selection
    def get_mode(self) -> int:
        return self.dev.call(F.ONBOARD_PROFILES, 0x2)[0]

    def set_mode(self, mode: int) -> None:
        self.dev.call(F.ONBOARD_PROFILES, 0x1, mode)

    def get_current_profile(self) -> int:
        """Return the active profile sector (1-based user profile, 0x01xx for ROM)."""
        d = self.dev.call(F.ONBOARD_PROFILES, 0x4)
        return (d[0] << 8) | d[1]

    def set_current_profile(self, sector: int) -> None:
        self.dev.call(F.ONBOARD_PROFILES, 0x3, sector >> 8, sector & 0xFF)

    def get_current_dpi_index(self) -> int:
        return self.dev.call(F.ONBOARD_PROFILES, 0xB)[0]

    def set_current_dpi_index(self, index: int) -> None:
        self.dev.call(F.ONBOARD_PROFILES, 0xC, index)

    # raw memory
    def read_sector(self, sector: int) -> bytes:
        size = self.info.sector_size
        out = bytearray(size)
        offset = 0
        while offset < size:
            o = min(offset, size - 16)  # reads may not run past the sector end
            chunk = self.dev.call(F.ONBOARD_PROFILES, 0x5, sector >> 8, sector & 0xFF, o >> 8, o & 0xFF)
            out[o : o + 16] = chunk[:16]
            offset += 16
        return bytes(out)

    def write_sector(self, sector: int, data: bytes, *, verify: bool = True) -> bool:
        """Write a full sector (CRC must already be in place). Returns False if unchanged."""
        size = self.info.sector_size
        if len(data) != size:
            raise ValueError(f"sector data must be {size} bytes, got {len(data)}")
        if sector >= ROM_BASE or sector >= self.info.sector_count:
            raise ValueError(f"sector {sector:#06x} is not writable")
        if not sector_valid(data):
            raise ValueError("sector CRC is wrong; refusing to write")
        try:
            current = self.read_sector(sector)
        except HIDPPError:  # never-written sectors can refuse reads
            current = None
        if current == data:
            return False
        self.dev.call(F.ONBOARD_PROFILES, 0x6, sector >> 8, sector & 0xFF, 0, 0, size >> 8, size & 0xFF)
        for o in range(0, size, 16):
            self.dev.call(F.ONBOARD_PROFILES, 0x7, data[o : o + 16].ljust(16, b"\x00"))
        self.dev.call(F.ONBOARD_PROFILES, 0x8)
        if verify:
            back = self.read_sector(sector)
            if back != data:
                raise IOError(f"verification of sector {sector:#06x} failed after write")
        return True

    # directory and profiles
    def read_directory(self) -> list[tuple[int, bool]]:
        data = self.read_sector(DIRECTORY)
        entries = []
        if sector_valid(data):
            for i in range(self.info.profile_count):
                sec = struct.unpack(">H", data[4 * i : 4 * i + 2])[0]
                if sec == 0xFFFF:
                    break
                entries.append((sec, bool(data[4 * i + 2])))
        return entries

    def write_directory(self, entries: list[tuple[int, bool]]) -> bool:
        current = self.read_sector(DIRECTORY)
        buf = bytearray(b"\xff" * self.info.sector_size)
        for i, (sec, enabled) in enumerate(entries):
            buf[4 * i : 4 * i + 4] = bytes([sec >> 8, sec & 0xFF, 1 if enabled else 0, current[4 * i + 3] if sector_valid(current) else 0xFF])
        n = len(entries)
        buf[4 * n : 4 * n + 2] = b"\xff\xff"
        return self.write_sector(DIRECTORY, seal(buf))

    def read_profile(self, sector: int, enabled: bool = True) -> OnboardProfile:
        return OnboardProfile(self.read_sector(sector), sector, enabled)

    def read_profiles(self) -> list[OnboardProfile]:
        """Read every user profile slot; slots with a bad CRC fall back to the first ROM profile."""
        directory = dict(self.read_directory())
        profiles = []
        for slot in range(1, self.info.profile_count + 1):
            data = self.read_sector(slot)
            if not sector_valid(data):
                rom = ROM_BASE + min(slot, max(1, self.info.rom_profile_count))
                data = self.read_sector(rom)
            profiles.append(OnboardProfile(data, slot, directory.get(slot, False)))
        return profiles

    def write_profile(self, profile: OnboardProfile) -> bool:
        if not 1 <= profile.sector <= self.info.profile_count:
            raise ValueError(f"profile slot {profile.sector} out of range")
        return self.write_sector(profile.sector, profile.to_bytes())

    def set_enabled(self, slot: int, enabled: bool) -> bool:
        entries = self.read_directory()
        if not entries:
            entries = [(i, i == 1) for i in range(1, self.info.profile_count + 1)]
        entries = [(s, enabled if s == slot else e) for s, e in entries]
        if slot not in [s for s, _ in entries]:
            entries.append((slot, enabled))
        return self.write_directory(entries)

    # macros
    def read_macro(self, sector: int, address: int) -> list[tuple[int, bytes]]:
        ops: list[tuple[int, bytes]] = []
        seen = set()
        while (sector, address) not in seen:
            seen.add((sector, address))
            mem = self.read_sector(sector)
            part = parse_macro_bytes(mem[:-2], address)
            if part and part[-1][0] == OP_JUMP:
                args = part[-1][1]
                ops += part[:-1]
                address, sector = args[0], args[1]  # libratbag: jump = offset, page
                continue
            ops += part
            break
        return ops

    def macro_sectors_in_use(self, profiles: list[OnboardProfile]) -> set[int]:
        used = set()
        for p in profiles:
            for gshift in (False, True):
                for a in p.buttons(16, gshift):
                    if a.kind == "macro":
                        used.add(a.sector)
        return used

    def free_macro_sectors(self, profiles: list[OnboardProfile]) -> list[int]:
        used = self.macro_sectors_in_use(profiles)
        first = self.info.profile_count + 1
        return [s for s in range(first, self.info.sector_count) if s not in used]

    def write_macro(self, sector: int, ops: list[tuple[int, bytes]]) -> None:
        body = encode_macro_ops(ops)
        if len(body) > self.info.sector_size - 2:
            raise ValueError(f"macro is {len(body)} bytes; at most {self.info.sector_size - 2} fit in a sector")
        buf = bytearray(b"\xff" * self.info.sector_size)
        buf[: len(body)] = body
        self.write_sector(sector, seal(buf))

    # backups
    def backup(self) -> dict:
        """Read every readable sector, user and ROM, into a JSON-friendly dict."""
        desc = self.dev.call(F.ONBOARD_PROFILES, 0x0)
        sectors = {}
        for s in list(range(0, self.info.sector_count)) + [ROM_BASE + i for i in range(1, self.info.rom_profile_count + 1)]:
            try:
                sectors[f"{s:#06x}"] = self.read_sector(s).hex()
            except Exception as e:  # unwritten sectors can refuse reads
                log.debug("sector %#06x unreadable: %s", s, e)
        return {
            "format": "logi-ghub-linux onboard backup v1",
            "taken": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "description": desc.hex(),
            "mode": self.get_mode(),
            "current_profile": self.get_current_profile(),
            "sectors": sectors,
        }

    def restore(self, backup: dict) -> list[int]:
        """Write back user sectors from a backup made by :meth:`backup`. Returns sectors changed."""
        if bytes.fromhex(backup["description"])[:11] != self.dev.call(F.ONBOARD_PROFILES, 0x0)[:11]:
            raise ValueError("backup was taken from a different device model")
        changed = []
        for key, hexdata in backup["sectors"].items():
            s = int(key, 16)
            if s >= ROM_BASE or s >= self.info.sector_count:
                continue
            data = bytes.fromhex(hexdata)
            if not sector_valid(data):
                continue  # never-written sectors; leave them alone
            if self.write_sector(s, data):
                changed.append(s)
        return changed


def backup_dir() -> Path:
    from ..config import data_dir

    d = data_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_backup(onboard: OnboardProfiles, model_key: str) -> Path:
    path = backup_dir() / f"{model_key.replace(':', '-')}-{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(onboard.backup(), indent=1))
    return path
