"""An in-memory HID++ 2.0 device, for tests and for running the app without hardware.

``FakeTransport`` answers requests the way the real firmware does, from a
feature table and per-feature handlers. The G502 X model is seeded with a real
on-board memory dump so profile parsing and writing can be exercised.
"""

from __future__ import annotations

import collections
import json
import struct
import time
from pathlib import Path

from . import features as F
from .transport import REPORT_LONG, REPORT_SHORT, HidrawInfo

ERR_INVALID_ARGUMENT = 0x02
ERR_INVALID_FUNCTION = 0x07


class FakeError(Exception):
    def __init__(self, code: int):
        self.code = code


class FakeTransport:
    def __init__(self, model: "FakeModel"):
        self.model = model
        self.path = f"fake:{model.key}"
        self.queue: collections.deque[bytes] = collections.deque()
        self.log: list[bytes] = []

    def fileno(self) -> int:
        return -1

    def write(self, data: bytes) -> None:
        self.log.append(bytes(data))
        report, devnumber, sub_id, address = data[0], data[1], data[2], data[3]
        params = bytes(data[4:])
        try:
            reply = self.model.handle(sub_id, address >> 4, params)
            self.queue.append(bytes([REPORT_LONG, 0x01, sub_id, address]) + reply.ljust(16, b"\x00")[:16])
        except FakeError as e:
            self.queue.append(bytes([REPORT_LONG, 0x01, 0xFF, sub_id, address, e.code]).ljust(20, b"\x00"))

    def read(self, timeout):
        if not self.queue and timeout:
            time.sleep(min(timeout, 0.02))
        return self.queue.popleft() if self.queue else None

    def notify(self, sub_id: int, function: int, data: bytes) -> None:
        self.queue.append(bytes([REPORT_LONG, 0x01, sub_id, function << 4]) + data.ljust(16, b"\x00"))

    def close(self) -> None:
        pass


class FakeModel:
    key = "046d:0000"
    name = "Fake device"
    kind = 3
    features: list[int] = []

    def __init__(self):
        self.table = [F.ROOT, F.FEATURE_SET] + [f for f in self.features if f not in (F.ROOT, F.FEATURE_SET)]

    def index(self, fid: int) -> int:
        return self.table.index(fid)

    def handle(self, sub_id: int, function: int, params: bytes) -> bytes:
        if sub_id >= len(self.table):
            raise FakeError(0x06)
        fid = self.table[sub_id]
        method = getattr(self, f"f_{fid:04x}", None)
        if method is None:
            raise FakeError(ERR_INVALID_FUNCTION)
        return method(function, params)

    def info(self) -> HidrawInfo:
        vid, pid = (int(x, 16) for x in self.key.split(":"))
        return HidrawInfo(f"fake:{self.key}", self.name, vid, pid, 3, "fake", frozenset({REPORT_SHORT, REPORT_LONG}), "fake")

    # root
    def f_0000(self, fn, p):
        if fn == 0:
            fid = struct.unpack(">H", p[0:2])[0]
            return bytes([self.table.index(fid), 0, 0]) if fid in self.table else b"\x00\x00\x00"
        if fn == 1:
            return bytes([4, 2, p[2]])
        raise FakeError(ERR_INVALID_FUNCTION)

    def f_0001(self, fn, p):
        if fn == 0:
            return bytes([len(self.table) - 1])
        if fn == 1:
            return struct.pack(">HBB", self.table[p[0]], 0, 0)
        raise FakeError(ERR_INVALID_FUNCTION)

    def f_0005(self, fn, p):
        name = self.name.encode()
        if fn == 0:
            return bytes([len(name)])
        if fn == 1:
            return name[p[0] : p[0] + 16]
        if fn == 2:
            return bytes([self.kind])
        raise FakeError(ERR_INVALID_FUNCTION)

    def f_0003(self, fn, p):
        if fn == 0:
            return bytes([1])
        if fn == 1:
            return bytes([0]) + b"FAK" + bytes([1, 2, 0, 3])
        raise FakeError(ERR_INVALID_FUNCTION)

    def f_1004(self, fn, p):
        if fn == 0:
            return bytes([0x0F, 0x0F, 0x01])
        if fn == 1:
            return bytes([77, 0x04, 0x00, 0x00])
        raise FakeError(ERR_INVALID_FUNCTION)


FIXTURE = Path(__file__).with_name("fake_g502x.json")


class FakeG502X(FakeModel):
    key = "046d:409f"
    name = "G502 X LIGHTSPEED"
    kind = 3
    features = [F.DEVICE_FW_VERSION, F.DEVICE_NAME, F.UNIFIED_BATTERY, F.ADJUSTABLE_DPI, F.ONBOARD_PROFILES,
                F.MOUSE_BUTTON_SPY, F.REPORT_RATE]

    def __init__(self, fixture: Path | None = None):
        super().__init__()
        dump = json.loads((fixture or FIXTURE).read_text())
        self.desc = bytes.fromhex(dump.get("desc") or dump["description"])
        self.sector_size = struct.unpack(">H", self.desc[7:9])[0]
        self.sector_count = self.desc[6]
        self.memory: dict[int, bytearray] = {int(k, 16): bytearray.fromhex(v) for k, v in dump["sectors"].items()}
        self.mode = 1
        self.profile = 1
        self.dpi = 2400
        self.rate_ms = 1
        self.spying = False
        self.remap = list(range(1, 12))
        self._write: tuple[int, int, int] | None = None
        self._write_buf = bytearray()
        self.writes: list[int] = []

    def f_2201(self, fn, p):
        if fn == 0:
            return bytes([1])
        if fn == 1:
            return bytes([0, 0x00, 0x64, 0xE0, 0x32, 0x64, 0x00])
        if fn == 2:
            return bytes([0]) + struct.pack(">HH", self.dpi, 1600)
        if fn == 3:
            self.dpi = struct.unpack(">H", p[1:3])[0]
            return p[:3]
        raise FakeError(ERR_INVALID_FUNCTION)

    def f_8060(self, fn, p):
        if fn == 0:
            return bytes([0x8B])
        if fn == 1:
            return bytes([self.rate_ms])
        if fn == 2:
            self.rate_ms = p[0]
            return b""
        raise FakeError(ERR_INVALID_FUNCTION)

    def f_8110(self, fn, p):
        if fn == 0:
            return bytes([11])
        if fn == 1:
            self.spying = True
            return b""
        if fn == 2:
            self.spying = False
            return b""
        if fn == 3:
            return bytes(self.remap)
        if fn == 4:
            self.remap = list(p[:11])
            return b""
        raise FakeError(ERR_INVALID_FUNCTION)

    def f_8100(self, fn, p):
        size = self.sector_size
        if fn == 0:
            return self.desc
        if fn == 1:
            if p[0]:
                self.mode = p[0]
            return b""
        if fn == 2:
            return bytes([self.mode])
        if fn == 3:
            self.profile = (p[0] << 8) | p[1]
            return b""
        if fn == 4:
            return bytes([self.profile >> 8, self.profile & 0xFF])
        if fn == 5:
            sector, offset = struct.unpack(">HH", p[0:4])
            if sector not in self.memory or offset + 16 > size:
                raise FakeError(ERR_INVALID_ARGUMENT)
            return bytes(self.memory[sector][offset : offset + 16])
        if fn == 6:
            sector, offset, length = struct.unpack(">HHH", p[0:6])
            if sector >= self.sector_count or offset + length > size:
                raise FakeError(ERR_INVALID_ARGUMENT)
            self._write = (sector, offset, length)
            self._write_buf = bytearray()
            return b""
        if fn == 7:
            if self._write is None:
                raise FakeError(ERR_INVALID_ARGUMENT)
            self._write_buf += p[:16]
            return b""
        if fn == 8:
            sector, offset, length = self._write
            mem = self.memory.setdefault(sector, bytearray(b"\xff" * size))
            mem[offset : offset + length] = self._write_buf[:length]
            self.writes.append(sector)
            self._write = None
            return b""
        if fn == 0xB:
            return bytes([3])
        if fn == 0xC:
            return b""
        raise FakeError(ERR_INVALID_FUNCTION)


class FakeKeyboard(FakeModel):
    key = "046d:40b6"
    name = "G915 X LS TKL (simulated)"
    kind = 0
    features = [F.DEVICE_FW_VERSION, F.DEVICE_NAME, F.UNIFIED_BATTERY, F.RGB_EFFECTS, F.PER_KEY_LIGHTING_V2,
                F.BRIGHTNESS_CONTROL]

    ZONE_EFFECTS = {0: [0x00, 0x01, 0x03, 0x0A, 0x0B, 0x16], 1: [0x00, 0x01, 0x0A]}

    def __init__(self):
        super().__init__()
        self.sw_control = (0, 0)
        self.effects: dict[int, bytes] = {}
        self.keys: dict[int, int] = {}
        self.pending: dict[int, int] = {}
        self.brightness = 100

    def f_8071(self, fn, p):
        if fn == 0:
            zone, effect = p[0], p[1]
            if p[2] == 0x01:  # stored ("onboard") effect slots, like the real keyboard's two G HUB effects
                names = [b"Main Keyboard", b"Gaming Lights"]
                slot, kind = p[3], p[4]
                if zone != 0xFF or slot >= len(names):
                    raise FakeError(ERR_INVALID_ARGUMENT)
                chunk = names[slot][(kind - 4) * 11:(kind - 3) * 11] if kind >= 4 else b""
                return bytes([0xFF, 0x00, 0x01, 0x00, 0x00]) + chunk.ljust(11, b"\x00")
            if zone == 0xFF:
                return bytes([0xFF, 0xFF, len(self.ZONE_EFFECTS), 0, 0, 0, 1])
            effects = self.ZONE_EFFECTS[zone]
            if effect == 0xFF:
                return bytes([zone, 0xFF, 0x00, 0x01 if zone == 0 else 0x02, len(effects), 0, 0])
            return bytes([zone, effect]) + struct.pack(">HHH", effects[effect], 0, 0)
        if fn == 1:
            self.effects[p[0]] = bytes(p[:13])
            return p[:2]
        if fn == 5:
            if p[0] == 1:
                self.sw_control = (p[1], p[2])
            return bytes([p[0], *self.sw_control])
        raise FakeError(ERR_INVALID_FUNCTION)

    def f_8081(self, fn, p):
        if fn == 0:
            # same zone bitmap as the real G915 X TKL (data/devices/046d-40b6.json)
            pages = {0: "ffffffffffffffffffffffffffff", 1: "00000000007a0000000000000400", 2: "0000000000000000000000000000"}
            return bytes([p[0], p[1]]) + bytes.fromhex(pages[p[1]])
        if fn == 1:
            for i in range(0, 16, 4):
                if p[i]:
                    self.pending[p[i]] = int.from_bytes(p[i + 1 : i + 4], "big")
            return b""
        if fn == 5:
            for z in range(p[0], p[1] + 1):
                self.pending[z] = int.from_bytes(p[2:5], "big")
            return b""
        if fn == 6:
            color = int.from_bytes(p[0:3], "big")
            for z in p[3:16]:
                if z:
                    self.pending[z] = color
            return b""
        if fn == 7:
            self.keys.update(self.pending)
            self.pending.clear()
            return b""
        raise FakeError(ERR_INVALID_FUNCTION)

    def f_8040(self, fn, p):
        if fn == 0:
            return struct.pack(">HBBH", 100, 0, 0, 0)
        if fn == 1:
            return struct.pack(">H", self.brightness)
        if fn == 2:
            self.brightness = struct.unpack(">H", p[0:2])[0]
            return b""
        raise FakeError(ERR_INVALID_FUNCTION)


def fake_devices():
    """Build LogiDevice objects backed by fake models (used by --fake)."""
    from ..device import LogiDevice
    from .protocol import Device

    out = []
    for model in (FakeG502X(), FakeKeyboard()):
        d = LogiDevice(model.info())
        d.hidpp = Device(FakeTransport(model))
        d.hidpp.ping()
        d.hidpp.discover_features()
        from ..features import basic

        d.name = basic.device_name(d.hidpp)
        d.kind = basic.device_kind(d.hidpp)
        d.online = True
        d.fake_model = model
        out.append(d)
    from ..features.g600 import fake_g600

    out.append(fake_g600())
    return out
