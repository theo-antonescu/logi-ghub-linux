"""Logitech G600: a USB mouse that predates HID++ 2.0 and is configured with HID feature reports.

Layout from libratbag's G600 driver, checked against a real G600.

* report 0xF0 (4 bytes): active profile and DPI stage. Read: byte 1 = profile << 4 | resolution << 1.
  Write 0x80 | profile << 4 selects a profile, 0x40 | stage << 1 selects a DPI stage.
* reports 0xF3, 0xF4, 0xF5 (154 bytes): the three on-board profiles::

    0      report id
    1..3   LED colour      4  LED effect (0 solid, 1 breathe, 2 colour cycle)   5  effect period (s)
    6..10  unknown         11 report rate (1000 / (n + 1) Hz)
    12     DPI shift (n * 50 DPI, 0 = off)   13 default DPI stage (1-4)   14..17 four DPI stages (n * 50)
    18..30 unknown
    31..90  20 buttons, 3 bytes each: code, modifiers, HID key
    91..93  LED colour while G-Shift is held
    94..153 20 G-Shift buttons
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..actions import Action
from ..hidpp.transport import HidrawInfo, _report_ids, _uevent

VID, PID = 0x046D, 0xC24A
REPORT_ACTIVE = 0xF0
PROFILE_REPORTS = (0xF3, 0xF4, 0xF5)
PROFILE_SIZE = 154
BUTTONS = 20
DPI_MIN, DPI_MAX, DPI_STEP = 200, 8200, 50
LED_EFFECTS = {0: "Solid", 1: "Breathing", 2: "Colour cycle"}

BUTTON_NAMES = ["Left click", "Right click", "Middle click (wheel)", "Wheel tilt left", "Wheel tilt right",
                "G-Shift (ring finger)", "G7 (behind wheel)", "G8 (behind wheel)"] + [f"G{n} (side)" for n in range(9, 21)]

# G600 button codes <-> actions
_CODE_TO_SPECIAL = {0x11: 0x03, 0x12: 0x04, 0x13: 0x05, 0x14: 0x0A, 0x15: 0x07, 0x17: 0x0B}
_SPECIAL_TO_CODE = {v: k for k, v in _CODE_TO_SPECIAL.items()}


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("H") << 8) | nr


def HIDIOCGFEATURE(size: int) -> int:
    return _ioc(3, 0x07, size)


def HIDIOCSFEATURE(size: int) -> int:
    return _ioc(3, 0x06, size)


def decode_button(b: bytes) -> Action:
    code, mods, key = b[0], b[1], b[2]
    if 1 <= code <= 5:
        return Action.mouse(code)
    if code in _CODE_TO_SPECIAL:
        return Action.special(_CODE_TO_SPECIAL[code])
    if code == 0x00:
        return Action("key", modifiers=mods, key=key) if (mods or key) else Action("none")
    return Action("raw", raw=bytes(b).hex())


def encode_button(a: Action) -> bytes:
    if a.kind == "mouse" and 1 <= a.button <= 5:
        return bytes([a.button, 0, 0])
    if a.kind == "special" and a.function in _SPECIAL_TO_CODE:
        return bytes([_SPECIAL_TO_CODE[a.function], 0, 0])
    if a.kind == "key":
        return bytes([0x00, a.modifiers & 0xFF, a.key & 0xFF])
    if a.kind == "none":
        return b"\x00\x00\x00"
    if a.kind == "raw":
        return bytes.fromhex(a.raw)[:3]
    raise ValueError(f"the G600 can't store “{a.label()}” on a button (it supports mouse buttons, keys, "
                     "DPI and profile functions and G-Shift)")


class G600Profile:
    """One profile report, kept raw; mirrors the parts of OnboardProfile the rest of the app uses."""

    max_dpi_stages = 4

    def __init__(self, data: bytes, slot: int):
        if len(data) != PROFILE_SIZE:
            raise ValueError("G600 profile must be 154 bytes")
        self.raw = bytearray(data)
        self.sector = slot
        self.enabled = True

    # DPI and rate
    @property
    def dpis(self) -> list[int]:
        return [self.raw[14 + i] * DPI_STEP for i in range(4)]

    @dpis.setter
    def dpis(self, values: list[int]) -> None:
        values = list(values)[:4] + [0] * (4 - min(4, len(values)))
        for i, v in enumerate(values):
            self.raw[14 + i] = 0 if not v else max(DPI_MIN, min(DPI_MAX, v)) // DPI_STEP

    @property
    def active_dpis(self) -> list[int]:
        return [d for d in self.dpis if d]

    @property
    def default_dpi_index(self) -> int:
        return max(0, self.raw[13] - 1)

    @default_dpi_index.setter
    def default_dpi_index(self, i: int) -> None:
        self.raw[13] = i + 1

    @property
    def shift_dpi(self) -> int:
        return self.raw[12] * DPI_STEP

    def set_shift_dpi(self, value: int) -> None:
        self.raw[12] = 0 if not value else max(DPI_MIN, min(DPI_MAX, value)) // DPI_STEP

    @property
    def shift_dpi_index(self) -> int:  # for code that expects an index into the stages
        return self.dpis.index(self.shift_dpi) if self.shift_dpi in self.dpis else 0

    @property
    def report_rate_ms(self) -> int:
        return self.raw[11] + 1

    @report_rate_ms.setter
    def report_rate_ms(self, ms: int) -> None:
        self.raw[11] = max(1, min(8, ms)) - 1

    @property
    def name(self) -> str:
        return ""

    @name.setter
    def name(self, value: str) -> None:  # the G600 has no profile names
        pass

    # lighting
    @property
    def led(self) -> dict:
        return {"effect": self.raw[4], "color": int.from_bytes(self.raw[1:4], "big"), "duration": self.raw[5],
                "gshift_color": int.from_bytes(self.raw[91:94], "big")}

    def set_led(self, effect: int, color: int, duration: int, gshift_color: int | None = None) -> None:
        self.raw[1:4] = color.to_bytes(3, "big")
        self.raw[4] = effect
        self.raw[5] = max(1, min(15, duration))
        if gshift_color is not None:
            self.raw[91:94] = gshift_color.to_bytes(3, "big")

    # buttons
    def button(self, i: int, gshift: bool = False) -> Action:
        off = (94 if gshift else 31) + 3 * i
        return decode_button(self.raw[off : off + 3])

    def set_button(self, i: int, action: Action, gshift: bool = False) -> None:
        off = (94 if gshift else 31) + 3 * i
        if action.kind == "default":
            return
        self.raw[off : off + 3] = encode_button(action)

    def buttons(self, count: int = BUTTONS, gshift: bool = False) -> list[Action]:
        return [self.button(i, gshift) for i in range(min(count, BUTTONS))]

    def to_bytes(self) -> bytes:
        return bytes(self.raw)

    def copy(self) -> "G600Profile":
        return G600Profile(bytes(self.raw), self.sector)


@dataclass
class G600Info:
    profile_count: int = 3
    rom_profile_count: int = 0
    button_count: int = BUTTONS
    has_gshift: bool = True
    has_dpi_shift: bool = True
    supports_macros: bool = False


class G600Memory:
    """On-board profiles of the G600, with the same methods the app uses for HID++ mice."""

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        self.info = G600Info()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _get(self, rid: int, size: int) -> bytes:
        buf = bytearray(size)
        buf[0] = rid
        fcntl.ioctl(self.fd, HIDIOCGFEATURE(size), buf)
        return bytes(buf)

    def _set(self, data: bytes) -> None:
        buf = bytearray(data)
        fcntl.ioctl(self.fd, HIDIOCSFEATURE(len(buf)), buf)

    # mode / selection (the G600 is always in on-board mode)
    def get_mode(self) -> int:
        return 1

    def set_mode(self, mode: int) -> None:
        pass

    def active(self) -> tuple[int, int]:
        """(profile 0-2, DPI stage 0-3)."""
        b = self._get(REPORT_ACTIVE, 4)[1]
        return (b >> 4) & 0x0F, (b >> 1) & 0x03

    def get_current_profile(self) -> int:
        return self.active()[0] + 1

    def set_current_profile(self, slot: int) -> None:
        self._set(bytes([REPORT_ACTIVE, 0x80 | ((slot - 1) << 4), 0, 0]))

    def set_dpi_stage(self, stage: int) -> None:
        self._set(bytes([REPORT_ACTIVE, 0x40 | (stage << 1), 0, 0]))

    # profiles
    def read_profile(self, slot: int, enabled: bool = True) -> G600Profile:
        return G600Profile(self._get(PROFILE_REPORTS[slot - 1], PROFILE_SIZE), slot)

    def read_profiles(self) -> list[G600Profile]:
        return [self.read_profile(s) for s in (1, 2, 3)]

    def read_directory(self) -> list[tuple[int, bool]]:
        return [(1, True), (2, True), (3, True)]

    def set_enabled(self, slot: int, enabled: bool) -> bool:
        return False

    # The G600 stores a profile slowly: a second report sent while it is still busy (or reading it back too
    # early) returns or stores shifted bytes. So writes are spaced out and the result is checked after a pause.
    SETTLE = 2.5

    def _settle(self) -> None:
        wait = getattr(self, "_last_write", 0.0) + self.SETTLE - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def write_profile(self, profile: G600Profile) -> bool:
        rid = PROFILE_REPORTS[profile.sector - 1]
        self._settle()
        data = bytearray(profile.to_bytes())
        data[0] = rid
        data = bytes(data)
        if self._get(rid, PROFILE_SIZE) == data:
            return False
        for _attempt in range(2):
            self._set(data)
            self._last_write = time.monotonic()
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                time.sleep(1.0 if self.SETTLE else 0.0)
                if self._get(rid, PROFILE_SIZE) == data:
                    self._last_write = time.monotonic()
                    return True
            self._settle()
        raise IOError(f"G600 profile {profile.sector} did not read back as written")

    # macros: the G600 has no on-board macro memory
    def free_macro_sectors(self, profiles) -> list[int]:
        return []

    def read_macro(self, sector: int, address: int):
        raise ValueError("the G600 has no on-board macros")

    # backups
    def backup(self) -> dict:
        profile, stage = self.active()
        return {
            "format": "logi-ghub-linux G600 backup v1",
            "taken": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "description": "g600",
            "active_profile": profile + 1,
            "active_dpi_stage": stage,
            "profiles": {str(i + 1): self._get(rid, PROFILE_SIZE).hex() for i, rid in enumerate(PROFILE_REPORTS)},
        }

    def restore(self, backup: dict) -> list[int]:
        if backup.get("description") != "g600":
            raise ValueError("this backup is not from a G600")
        changed = []
        for slot, hexdata in backup["profiles"].items():
            if self.write_profile(G600Profile(bytes.fromhex(hexdata), int(slot))):
                changed.append(int(slot))
        return changed


class G600Dpi:
    def __init__(self, memory: G600Memory):
        self.memory = memory

    def dpi_list(self, sensor: int = 0) -> list[int]:
        return list(range(DPI_MIN, DPI_MAX + 1, DPI_STEP))

    def get(self, sensor: int = 0) -> tuple[int, int]:
        profile, stage = self.memory.active()
        p = self.memory.read_profile(profile + 1)
        return p.dpis[stage] or p.dpis[p.default_dpi_index], p.dpis[p.default_dpi_index]

    def set(self, dpi: int, sensor: int = 0) -> None:
        profile, _ = self.memory.active()
        p = self.memory.read_profile(profile + 1)
        if dpi not in p.dpis:
            raise ValueError("the G600 can only switch to one of the DPI stages saved in the active profile")
        self.memory.set_dpi_stage(p.dpis.index(dpi))


class G600ReportRate:
    def __init__(self, memory: G600Memory):
        self.memory = memory

    def supported_hz(self) -> list[int]:
        return [125, 250, 500, 1000]

    def get_hz(self) -> int:
        profile, _ = self.memory.active()
        return 1000 // self.memory.read_profile(profile + 1).report_rate_ms

    def set_hz(self, hz: int) -> None:
        raise ValueError("the G600's report rate is part of its profiles; change it there")


class G600Device:
    """Duck-types LogiDevice for the GUI, CLI and profile code."""

    kind = "mouse"
    hidpp = None
    rgb = per_key = brightness = button_spy = gkeys = None
    model_ids: list[int] = []

    def __init__(self, info: HidrawInfo):
        self.info = info
        self.name = "G600"
        self.online = False
        self.error: str | None = None
        self.onboard: G600Memory | None = None
        self.dpi = self.report_rate = None

    @property
    def key(self) -> str:
        return self.info.key

    def open(self) -> "G600Device":
        try:
            self.onboard = G600Memory(self.info.path)
            self.onboard.active()
            self.dpi = G600Dpi(self.onboard)
            self.report_rate = G600ReportRate(self.onboard)
            self.online = True
        except OSError as e:
            self.error = str(e)
            self.online = False
        return self

    def close(self) -> None:
        if self.onboard:
            self.onboard.close()
            self.onboard = None

    def has(self, feature_id: int) -> bool:
        return False

    def battery(self):
        return None

    def firmware(self):
        return []

    def button_name(self, index: int) -> str:
        return BUTTON_NAMES[index] if index < len(BUTTON_NAMES) else f"Button {index + 1}"

    def binding_count(self) -> int:
        return BUTTONS

    def describe(self) -> dict:
        out = {"name": self.name, "model": self.key, "path": self.info.path, "kind": self.kind, "online": self.online,
               "protocol": "HID feature reports (pre-HID++ G600)", "features": {}, "firmware": []}
        if not self.online:
            out["error"] = self.error
        return out


def enumerate_g600(sys_root: Path = Path("/sys/class/hidraw")) -> list[HidrawInfo]:
    found = []
    if not sys_root.exists():
        return found
    for node in sorted(sys_root.iterdir()):
        dev = node / "device"
        ue = _uevent(dev / "uevent")
        try:
            bus, vid, pid = (int(x, 16) for x in ue.get("HID_ID", "").split(":"))
        except ValueError:
            continue
        if (vid, pid) != (VID, PID):
            continue
        try:
            ids = _report_ids((dev / "report_descriptor").read_bytes())
        except OSError:
            continue
        if REPORT_ACTIVE in ids:
            found.append(HidrawInfo(f"/dev/{node.name}", ue.get("HID_NAME", "Logitech G600"), vid, pid, bus,
                                    "hid-generic", ids, ue.get("HID_PHYS", "")))
    return found


def save_backup_json(memory: G600Memory, path: Path) -> None:
    path.write_text(json.dumps(memory.backup(), indent=1))


class FakeG600Memory(G600Memory):
    """G600 memory backed by a dict, seeded with a real dump (tests and --fake)."""

    SETTLE = 0.0

    def __init__(self, fixture: Path | None = None):
        dump = json.loads((fixture or Path(__file__).resolve().parents[1] / "hidpp" / "fake_g600.json").read_text())
        self.reports = {PROFILE_REPORTS[int(k) - 1]: bytearray.fromhex(v) for k, v in dump["g600_profiles"].items()}
        self.reports[REPORT_ACTIVE] = bytearray([REPORT_ACTIVE, (dump["active"][0] - 1) << 4 | dump["active"][1] << 1, 0, 0])
        self.fd = -1
        self.info = G600Info()
        self.path = "fake:g600"

    def _get(self, rid: int, size: int) -> bytes:
        return bytes(self.reports[rid][:size])

    def _set(self, data: bytes) -> None:
        rid = data[0]
        if rid == REPORT_ACTIVE:
            b = data[1]
            cur = self.reports[REPORT_ACTIVE]
            if b & 0x80:
                cur[1] = (cur[1] & 0x0F) | (b & 0x70)
            if b & 0x40:
                cur[1] = (cur[1] & 0xF0) | (b & 0x06)
        else:
            self.reports[rid] = bytearray(data)

    def close(self) -> None:
        pass


def fake_g600() -> G600Device:
    info = HidrawInfo("fake:g600", "Logitech G600 (simulated)", VID, PID, 3, "fake", frozenset({REPORT_ACTIVE}), "fake")
    dev = G600Device(info)
    dev.onboard = FakeG600Memory()
    dev.dpi = G600Dpi(dev.onboard)
    dev.report_rate = G600ReportRate(dev.onboard)
    dev.name = "G600 (simulated)"
    dev.online = True
    return dev
