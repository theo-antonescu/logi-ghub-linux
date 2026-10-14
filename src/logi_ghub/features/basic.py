"""Small read-mostly features: name, firmware, battery, DPI, report rate, wheel."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..hidpp import Device
from ..hidpp import features as F

DEVICE_KINDS = {0: "keyboard", 1: "remote control", 2: "numpad", 3: "mouse", 4: "trackpad", 5: "trackball",
                6: "presenter", 7: "receiver", 8: "headset", 9: "webcam", 10: "steering wheel",
                11: "joystick", 12: "gamepad", 13: "dock", 14: "speaker", 15: "microphone"}


def device_name(dev: Device) -> str:
    length = dev.call(F.DEVICE_NAME, 0x0)[0]
    name = b""
    while len(name) < length:
        chunk = dev.call(F.DEVICE_NAME, 0x1, len(name))
        if not chunk.strip(b"\x00"):
            break
        name += chunk
    return name[:length].decode("utf-8", errors="replace")


def device_kind(dev: Device) -> str:
    try:
        return DEVICE_KINDS.get(dev.call(F.DEVICE_NAME, 0x2)[0], "device")
    except Exception:
        return "device"


@dataclass
class Firmware:
    kind: str
    name: str
    version: str


def firmware(dev: Device) -> list[Firmware]:
    out = []
    count = dev.call(F.DEVICE_FW_VERSION, 0x0)[0]
    for i in range(count):
        d = dev.call(F.DEVICE_FW_VERSION, 0x1, i)
        level = d[0] & 0x0F
        if level in (0, 1):
            name = d[1:4].decode("ascii", errors="replace")
            version = f"{d[4]:02X}.{d[5]:02X}"
            build = struct.unpack(">H", d[6:8])[0]
            if build:
                version += f".B{build:04X}"
            out.append(Firmware("firmware" if level == 0 else "bootloader", name, version))
        elif level == 2:
            out.append(Firmware("hardware", "", str(d[1])))
    return out


def model_ids(dev: Device) -> tuple[int | None, list[int]]:
    """(unit id, product ids by transport) from getEntityCount; the same physical device reports the
    same list whether it is plugged in by USB or reached through its receiver."""
    d = dev.call(F.DEVICE_FW_VERSION, 0x0)
    if len(d) < 13:
        return None, []
    unit = struct.unpack(">I", d[1:5])[0]
    transports = struct.unpack(">H", d[5:7])[0]
    ids = [struct.unpack(">H", d[7 + 2 * i : 9 + 2 * i])[0] for i in range(3)]
    by_bit = {}
    for bit in range(4):  # BT, BLE, Lightspeed/eQuad, USB
        if transports & (1 << bit) and ids:
            by_bit[bit] = ids.pop(0)
    ordered = ([by_bit[2]] if 2 in by_bit else []) + [v for b, v in sorted(by_bit.items()) if b != 2]
    return unit, [i for i in ordered if i]


@dataclass
class Battery:
    percent: int | None
    level: str
    charging: str


_LEVELS = {8: "full", 4: "good", 2: "low", 1: "critical"}
_CHARGING = {0: "discharging", 1: "charging", 2: "charging slowly", 3: "fully charged", 4: "charging error"}


def battery(dev: Device) -> Battery | None:
    if dev.has(F.UNIFIED_BATTERY):
        d = dev.call(F.UNIFIED_BATTERY, 0x1)
        level = next((v for k, v in _LEVELS.items() if d[1] & k), "unknown")
        return Battery(d[0] or None, level, _CHARGING.get(d[2], "unknown"))
    if dev.has(F.BATTERY_STATUS):
        d = dev.call(F.BATTERY_STATUS, 0x0)
        return Battery(d[0] or None, "", {0: "discharging", 1: "charging", 2: "charging", 3: "fully charged"}.get(d[2], "unknown"))
    return None


class AdjustableDpi:
    """0x2201: one or more sensors with a list or range of resolutions."""

    def __init__(self, dev: Device):
        self.dev = dev

    def sensor_count(self) -> int:
        return self.dev.call(F.ADJUSTABLE_DPI, 0x0)[0]

    def dpi_list(self, sensor: int = 0) -> list[int]:
        raw = self.dev.call(F.ADJUSTABLE_DPI, 0x1, sensor)[1:]
        values: list[int] = []
        i = 0
        while i + 1 < len(raw):
            v = struct.unpack(">H", raw[i : i + 2])[0]
            if v == 0:
                break
            if v >> 13 == 0b111:  # range: step, then upper bound
                step = v & 0x1FFF
                last = struct.unpack(">H", raw[i + 2 : i + 4])[0]
                values += list(range(values[-1] + step, last + 1, step))
                i += 4
            else:
                values.append(v)
                i += 2
        return values

    def get(self, sensor: int = 0) -> tuple[int, int]:
        """Return (current, default) DPI."""
        d = self.dev.call(F.ADJUSTABLE_DPI, 0x2, sensor)
        cur, default = struct.unpack(">HH", d[1:5])
        return (cur or default), default

    def set(self, dpi: int, sensor: int = 0) -> None:
        self.dev.call(F.ADJUSTABLE_DPI, 0x3, sensor, dpi >> 8, dpi & 0xFF)


class ReportRate:
    """0x8060 (milliseconds) or 0x8061 (extended rates up to 8 kHz)."""

    EXTENDED = {0: 125, 1: 250, 2: 500, 3: 1000, 4: 2000, 5: 4000, 6: 8000}  # index -> Hz

    def __init__(self, dev: Device):
        self.dev = dev
        self.extended = dev.has(F.EXTENDED_ADJUSTABLE_REPORT_RATE)
        if not self.extended and not dev.has(F.REPORT_RATE):
            raise LookupError("no report rate feature")

    def supported_hz(self) -> list[int]:
        if self.extended:
            flags = struct.unpack(">H", self.dev.call(F.EXTENDED_ADJUSTABLE_REPORT_RATE, 0x1)[0:2])[0]
            return [hz for i, hz in self.EXTENDED.items() if flags & (1 << i)]
        flags = self.dev.call(F.REPORT_RATE, 0x0)[0]
        return sorted((1000 // (i + 1) for i in range(8) if flags & (1 << i)))

    def get_hz(self) -> int:
        if self.extended:
            return self.EXTENDED[self.dev.call(F.EXTENDED_ADJUSTABLE_REPORT_RATE, 0x2)[0]]
        return 1000 // max(1, self.dev.call(F.REPORT_RATE, 0x1)[0])

    def set_hz(self, hz: int) -> None:
        if self.extended:
            idx = {v: k for k, v in self.EXTENDED.items()}[hz]
            self.dev.call(F.EXTENDED_ADJUSTABLE_REPORT_RATE, 0x3, idx)
        else:
            self.dev.call(F.REPORT_RATE, 0x2, round(1000 / hz))
