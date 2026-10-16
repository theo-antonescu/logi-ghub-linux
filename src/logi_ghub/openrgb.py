"""A small client for the OpenRGB SDK server, for lighting that isn't Logitech's.

OpenRGB (https://openrgb.org) has drivers for motherboards, RAM, GPUs, coolers and much more. Run it
with its SDK server on (``openrgb --server``, port 6742) and this client can list its devices and set
their LEDs, so the app can paint its lighting designs onto the rest of the computer.

Protocol: every packet is ``"ORGB" device(u32) id(u32) size(u32)`` and a payload, little-endian.
This client speaks protocol version 3 (modes have brightness; zones have no segments).
"""

from __future__ import annotations

import socket
import struct
import threading
from dataclasses import dataclass, field

PORT = 6742
PROTOCOL = 3

REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
REQUEST_PROTOCOL_VERSION = 40
SET_CLIENT_NAME = 50
UPDATE_LEDS = 1050
UPDATE_ZONE_LEDS = 1051
SET_CUSTOM_MODE = 1100
UPDATE_MODE = 1101

DEVICE_TYPES = ["Motherboard", "DRAM", "GPU", "Cooler", "LED strip", "Keyboard", "Mouse", "Mouse mat", "Headset",
                "Headset stand", "Gamepad", "Light", "Speaker", "Virtual", "Storage", "Case", "Microphone", "Accessory",
                "Keypad"]


@dataclass
class Zone:
    name: str
    type: int
    leds: int
    matrix: list[list[int]] | None = None  # rows of LED indexes (0xFFFFFFFF = no LED) when the zone is a grid


@dataclass
class Controller:
    index: int
    name: str
    type: int
    vendor: str
    description: str
    location: str
    zones: list[Zone] = field(default_factory=list)
    led_names: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    active_mode: int = 0
    mode_data: list[bytes] = field(default_factory=list)  # each mode as sent, for UPDATE_MODE

    @property
    def kind(self) -> str:
        return DEVICE_TYPES[self.type] if 0 <= self.type < len(DEVICE_TYPES) else "Device"

    @property
    def led_count(self) -> int:
        return len(self.led_names)


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, fmt: str):
        size = struct.calcsize(fmt)
        value = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += size
        return value if len(value) > 1 else value[0]

    def string(self) -> str:
        n = self.take("H")
        raw = self.data[self.pos : self.pos + n]
        self.pos += n
        return raw.split(b"\0")[0].decode("utf-8", "replace")

    def colors(self) -> list[int]:
        n = self.take("H")
        out = []
        for _ in range(n):
            r, g, b, _x = self.take("BBBB")
            out.append((r << 16) | (g << 8) | b)
        return out


def parse_controller(index: int, data: bytes, protocol: int = PROTOCOL) -> Controller:
    r = _Reader(data)
    r.take("I")  # data size
    ctype = r.take("i")
    name = r.string()
    vendor = r.string() if protocol >= 1 else ""
    description = r.string()
    r.string()  # version
    r.string()  # serial
    location = r.string()
    num_modes = r.take("H")
    active = r.take("i")
    modes, mode_data = [], []
    for _ in range(num_modes):
        start = r.pos
        modes.append(r.string())
        r.take("iIII")  # value, flags, speed min, speed max
        if protocol >= 3:
            r.take("II")  # brightness min, max
        r.take("III")  # colours min, max, speed
        if protocol >= 3:
            r.take("I")  # brightness
        r.take("II")  # direction, colour mode
        r.colors()
        mode_data.append(r.data[start : r.pos])
    zones = []
    for _ in range(r.take("H")):
        zname = r.string()
        ztype = r.take("i")
        r.take("II")  # leds min, max
        count = r.take("I")
        matrix = None
        matrix_len = r.take("H")
        if matrix_len:
            height, width = r.take("II")
            cells = [r.take("I") for _ in range(height * width)]
            matrix = [cells[y * width : (y + 1) * width] for y in range(height)]
        if protocol >= 4:
            for _ in range(r.take("H")):
                r.string()
                r.take("iII")
        zones.append(Zone(zname, ztype, count, matrix))
    leds = []
    for _ in range(r.take("H")):
        leds.append(r.string())
        r.take("I")
    return Controller(index, name, ctype, vendor, description, location, zones, leds, modes, active, mode_data)


class OpenRGB:
    """A connection to an OpenRGB SDK server. Thread-safe for whole requests."""

    def __init__(self, host: str = "127.0.0.1", port: int = PORT, name: str = "logi-ghub-linux", timeout: float = 3.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.lock = threading.Lock()
        self.protocol = 0
        server = struct.unpack("<I", self._request(0, REQUEST_PROTOCOL_VERSION, struct.pack("<I", PROTOCOL))[:4])[0]
        self.protocol = min(server, PROTOCOL)
        self._send(0, SET_CLIENT_NAME, name.encode() + b"\0")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- wire ------------------------------------------------------------------------------
    def _send(self, device: int, pkt: int, payload: bytes = b"") -> None:
        with self.lock:
            self.sock.sendall(b"ORGB" + struct.pack("<III", device, pkt, len(payload)) + payload)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("OpenRGB closed the connection")
            buf += chunk
        return buf

    def _request(self, device: int, pkt: int, payload: bytes = b"") -> bytes:
        with self.lock:
            self.sock.sendall(b"ORGB" + struct.pack("<III", device, pkt, len(payload)) + payload)
            while True:
                head = self._recv_exact(16)
                if head[:4] != b"ORGB":
                    raise ConnectionError("not an OpenRGB server")
                _dev, rid, size = struct.unpack("<III", head[4:])
                body = self._recv_exact(size)
                if rid == pkt:
                    return body
                # anything else (e.g. a device-list-changed notice) is skipped

    # -- API ---------------------------------------------------------------------------------
    def controller_count(self) -> int:
        return struct.unpack("<I", self._request(0, REQUEST_CONTROLLER_COUNT)[:4])[0]

    def controllers(self) -> list[Controller]:
        count = self.controller_count()
        out = []
        for i in range(count):
            data = self._request(i, REQUEST_CONTROLLER_DATA, struct.pack("<I", self.protocol))
            out.append(parse_controller(i, data, self.protocol))
        return out

    def set_custom_mode(self, device: int) -> None:
        """Switch a device to the mode where the computer sets each LED ("Direct" or "Custom")."""
        self._send(device, SET_CUSTOM_MODE)

    def set_mode(self, controller: Controller, name: str = "Direct") -> None:
        """Switch to a mode by name. Some devices (ASUS/ENE RAM and GPUs) only change with this, not
        with set_custom_mode."""
        i = next(k for k, m in enumerate(controller.modes) if m.lower() == name.lower())
        body = struct.pack("<i", i) + controller.mode_data[i]
        self._send(controller.index, UPDATE_MODE, struct.pack("<I", len(body) + 4) + body)

    def update_leds(self, device: int, colors: list[int]) -> None:
        """Set all of a device's LEDs, in order (0xRRGGBB each)."""
        body = struct.pack("<H", len(colors)) + b"".join(
            bytes([(c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF, 0]) for c in colors)
        self._send(device, UPDATE_LEDS, struct.pack("<I", len(body) + 4) + body)
