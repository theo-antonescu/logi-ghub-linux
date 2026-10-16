"""Storing custom lighting animations in a keyboard (G915 X), the way G HUB does.

Learned from a USB recording of G HUB saving an effect (tools/ghub-capture). The keyboard
keeps custom animations in slots; brightness + 8 plays slot 0 and brightness + 9 slot 1.
An animation is a file G HUB uploads with the DFU feature (0x00D0):

* DFU fn 4 ``03 01 "RGB_V0000"`` (brightness + 8) or ``04 01 "RGB_V0000"`` (brightness + 9)
  starts the upload; the keyboard answers with a notification when it is ready.
* the file follows in 16-byte packets; packet n (from 1) goes out on function n % 4, and
  each reply echoes n with a status (1 = continue, 2 = complete).

File layout::

    0x00  01 00 00 00 00 00 00 30, 8 zero bytes
    0x10  name, 32 bytes ASCII
    0x30  UUID, 16 bytes
    0x40  frame count (u16), 00 00, 00 00, frame count (u16), frame period ms (u16), brightness %
    0x4b  records of 17 bytes: a per-key lighting (0x8081) command byte and its 16 parameters,
          exactly as they would be sent. The command byte is function << 4 | software id (G HUB
          has used both 0xB and 0xA): fn 1 individual keys, fn 2 five consecutive keys, fn 5
          ranges, fn 6 one colour for up to 13 keys, and fn 7 ends a frame: 00, frame index (u16),
          duration in periods (u16)
    ...   0xFF padding, then the trailer packet 03 00 .. 00. Bytes 6-7 of the header are the padded
          size in 256-byte units, without the 32 bytes of header line and trailer (G HUB sizes
          files to fit). Each slot takes up to 400 units (102432 bytes); larger files are refused
          at the first packet with status 0x9a.

Only slot 1 is ever written by this program; slot 0 is refused.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
import uuid

from ..hidpp import Device, HIDPPError, Message
from ..hidpp import features as F

log = logging.getLogger(__name__)

FILE_SIZE = 12320  # what is transferred: the animation padded to 12304 bytes, then a 16-byte trailer
LARGE_FILE_SIZE = 102432  # the most each slot takes (measured): 400 x 256 bytes + 32; 102688 is refused
BODY_SIZE = FILE_SIZE - 16
TRAILER = b"\x03" + bytes(15)  # G HUB ends every upload with this packet; without it the keyboard refuses the file
HEADER_SIZE = 0x4B
RECORD = 17
MAX_RECORDS = (BODY_SIZE - HEADER_SIZE) // RECORD
WRITABLE_SLOTS = (1,)  # brightness + 9; slot 0 (brightness + 8) only when the owner explicitly asks (allow_slot0)
OP_INDIVIDUAL, OP_RANGE, OP_SINGLE_VALUE, OP_FRAME_END = 0x1B, 0x5B, 0x6B, 0x7B
FN_INDIVIDUAL, FN_CONSECUTIVE, FN_RANGE, FN_SINGLE_VALUE, FN_FRAME_END = 1, 2, 5, 6, 7


def _ranges(zones: list[int]) -> list[tuple[int, int]]:
    out = []
    for z in sorted(zones):
        if out and z == out[-1][1] + 1:
            out[-1] = (out[-1][0], z)
        else:
            out.append((z, z))
    return out


def encode_frame(colors: dict[int, int]) -> list[bytes]:
    """Per-key commands (17-byte records, without the frame end) that paint ``colors``."""
    by_color: dict[int, list[int]] = {}
    for z, c in colors.items():
        by_color.setdefault(c, []).append(z)
    records: list[bytes] = []
    range_items: list[bytes] = []  # 5 bytes each, 3 per record
    single_items: list[bytes] = []  # 4 bytes each, 4 per record
    for c, zones in by_color.items():
        rgb = c.to_bytes(3, "big")
        runs = _ranges(zones)
        # long runs are cheapest as ranges; scattered keys as one-colour lists of up to 13
        scattered = [z for a, b in runs if b - a < 2 for z in range(a, b + 1)]
        for a, b in runs:
            if b - a >= 2:
                range_items.append(bytes([a, b]) + rgb)
        if len(scattered) > 3:
            for i in range(0, len(scattered), 13):
                chunk = scattered[i : i + 13]
                records.append(bytes([OP_SINGLE_VALUE]) + rgb + bytes(chunk).ljust(13, b"\x00"))
        else:
            single_items += [bytes([z]) + rgb for z in scattered]
    for i in range(0, len(range_items), 3):
        records.append(bytes([OP_RANGE]) + b"".join(range_items[i : i + 3]).ljust(16, b"\x00"))
    for i in range(0, len(single_items), 4):
        records.append(bytes([OP_INDIVIDUAL]) + b"".join(single_items[i : i + 4]).ljust(16, b"\x00"))
    return records


def build_file(name: str, frames: list[tuple[dict[int, int], int]], period_ms: int = 32, brightness: int = 100,
               uid: bytes | None = None, max_size: int = FILE_SIZE) -> bytes:
    """``frames``: [(zone -> colour, duration in periods)]. Raises ValueError if it doesn't fit.

    Files are padded to ``FILE_SIZE`` like G HUB's usual size; with a smaller ``max_size`` (G HUB sized
    the brightness + 8 slot's file to its content) the file is only as large as it needs to be.
    """
    records: list[bytes] = []
    index = 0
    previous: dict[int, int] = {}
    for colors, duration in frames:
        changed = {z: c for z, c in colors.items() if previous.get(z) != c} if index else colors
        records += encode_frame(changed)
        records.append(bytes([OP_FRAME_END, 0x00]) + struct.pack(">HH", index, max(1, duration)) + bytes(11))
        previous = dict(colors)
        index += 1
    limit = (max_size - 16 - 16 - HEADER_SIZE) // RECORD
    if len(records) > limit:
        raise ValueError(f"animation needs {len(records)} commands; this slot stores at most {limit}")
    used = HEADER_SIZE + len(records) * RECORD
    units = 0x30 if max_size == FILE_SIZE else -(-(used - 16) // 256)  # body = units * 256 + 16
    header = bytearray(HEADER_SIZE)
    header[0], header[6], header[7] = 0x01, units >> 8, units & 0xFF  # bytes 6-7: size in 256-byte units
    header[0x10:0x30] = name.encode("ascii", "replace")[:31].ljust(32, b"\x00")
    header[0x30:0x40] = uid or uuid.uuid4().bytes
    header[0x40:0x4B] = struct.pack(">HHHHHB", index, 0, 0, index, period_ms, brightness)
    data = bytes(header) + b"".join(records)
    return data + b"\xff" * (units * 256 + 16 - len(data)) + TRAILER


def parse_file(data: bytes) -> dict:
    """Decode a file (as build_file makes it, or as recorded from G HUB) back into frames."""
    name = data[0x10:0x30].split(b"\x00")[0].decode("ascii", "replace")
    frame_count, _a, _b, length, period, brightness = struct.unpack(">HHHHHB", data[0x40:0x4B])
    frames: list[tuple[dict[int, int], int]] = []
    state: dict[int, int] = {}
    for off in range(HEADER_SIZE, len(data) - RECORD + 1, RECORD):
        r = data[off : off + RECORD]
        fn, p = r[0] >> 4, r[1:]
        if r == b"\xff" * RECORD:
            break
        if fn == FN_FRAME_END:
            frames.append((dict(state), struct.unpack(">H", p[3:5])[0]))
        elif fn == FN_SINGLE_VALUE:
            c = int.from_bytes(p[0:3], "big")
            for z in p[3:]:
                if z:
                    state[z] = c
        elif fn == FN_RANGE:
            for i in range(0, 15, 5):
                a, b = p[i], p[i + 1]
                if a or b:
                    for z in range(a, b + 1):
                        state[z] = int.from_bytes(p[i + 2 : i + 5], "big")
        elif fn == FN_CONSECUTIVE:
            for i in range(5):
                if p[0] + i < 256:
                    state[p[0] + i] = int.from_bytes(p[1 + 3 * i : 4 + 3 * i], "big")
        elif fn == FN_INDIVIDUAL:
            for i in range(0, 16, 4):
                if p[i]:
                    state[p[i]] = int.from_bytes(p[i + 1 : i + 4], "big")
    return {"name": name, "uuid": data[0x30:0x40].hex(), "frames": frames, "period_ms": period,
            "brightness": brightness, "frame_count": frame_count, "length": length}


def frames_from_lighting(light: dict, zones: list[int], period_ms: int = 32) -> list[tuple[dict[int, int], int]]:
    """Sample the app's lighting (painted keys and/or a colour sequence) into stored frames."""
    from ..lightfx import Renderer

    r = Renderer(light, zones)
    anim = r.animation
    if not anim:
        if r.is_animated():
            raise ValueError("the keyboard plays its built-in effects by itself; to save one, paint keys or "
                             "make a colour sequence instead")
        frame = {z: (0 if c == 0x202020 else c) for z, c in r.frame(0.0).items() if z in zones}  # unknown = off
        if not frame or set(frame.values()) == {0}:
            raise ValueError("there is nothing to save yet: paint some keys or set up a colour sequence")
        return [(frame, 1)]
    loop_ms = max(1, len(anim["colors"])) * max(50, int(anim.get("step_ms", 1000)))
    frames: list[tuple[dict[int, int], int]] = []
    # stepped sequences need one frame per colour; fades are sampled, coarser if they don't fit
    for every in (1, 2, 3, 4, 6, 8, 12, 16):
        step = period_ms * every
        frames = []
        t = 0
        while t < loop_ms:
            colors = {z: c for z, c in r.frame(t / 1000).items() if z in zones}
            if frames and frames[-1][0] == colors:
                frames[-1] = (colors, frames[-1][1] + every)
            else:
                frames.append((colors, every))
            t += step
        try:
            build_file("probe", frames, period_ms)
            return frames
        except ValueError:
            continue
    raise ValueError("this animation is too detailed to store on the keyboard; use fewer colours or the whole-keyboard pattern")


class Uploader:
    """Sends a file to a storage slot with the DFU feature, checking every packet's reply."""

    def __init__(self, dev: Device):
        self.dev = dev
        self.index = dev.get_feature_index(F.DFU)
        self._ready = threading.Event()

    def _notify(self, msg: Message) -> None:
        if msg.sub_id == self.index and msg.swid == 0:
            self._ready.set()

    def upload(self, slot: int, data: bytes, progress=None, allow_slot0: bool = False) -> None:
        if slot not in WRITABLE_SLOTS and not (slot == 0 and allow_slot0):
            raise ValueError(f"refusing to write slot {slot}: only slot 1 (brightness + 9) may be changed")
        if len(data) % 16 or len(data) < HEADER_SIZE + 16 or data[0] != 0x01 or data[-16:] != TRAILER:
            raise ValueError("not an RGB animation file")
        self._ready.clear()  # wait for this upload's own "ready" notification
        self.dev.add_handler(self._notify)
        try:
            # G HUB starts an upload with 03 01 for brightness + 8 (slot 0) and 04 01 for brightness + 9
            reply = self.dev.request(self.index, 4, bytes([0x03 + slot, 0x01]) + b"RGB_V0000".ljust(14, b"\x00"), timeout=5)
            if reply[4] not in (0x01, 0x03):
                raise IOError(f"keyboard refused the upload (status {reply[4]:#04x})")
            if not self._ready.is_set():  # G HUB waits for this notification (about two seconds)
                deadline = time.monotonic() + 8
                while not self._ready.is_set() and time.monotonic() < deadline:
                    self.dev.poll(0.2)
            packets = len(data) // 16
            for n in range(1, packets + 1):
                chunk = data[(n - 1) * 16 : n * 16]
                reply = self.dev.request(self.index, n % 4, chunk, timeout=5, retries=0)
                seq = struct.unpack(">H", reply[2:4])[0]
                status = reply[4]
                if seq != n or status not in (0x01, 0x02):
                    raise IOError(f"upload stopped at packet {n}: reply {reply[:5].hex(' ')}")
                if status == 0x02 and n != packets:
                    raise IOError(f"keyboard ended the upload early at packet {n}")
                if progress and n % 32 == 0:
                    progress(n / packets)
            if status != 0x02:
                raise IOError("keyboard did not confirm the upload")
        except HIDPPError as e:
            raise IOError(f"upload failed: {e}") from e
        finally:
            self.dev.remove_handler(self._notify)


# The keyboard can't send stored animations back, so keep a copy of what this program saved.
def _local_path(model_key: str, slot: int):
    from ..config import data_dir

    d = data_dir() / "stored-animations"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{model_key.replace(':', '-')}-slot{slot}.bin"


def save_local(model_key: str, slot: int, data: bytes) -> None:
    _local_path(model_key, slot).write_bytes(data)


def load_local(model_key: str, slot: int) -> dict | None:
    try:
        return parse_file(_local_path(model_key, slot).read_bytes())
    except (OSError, ValueError, struct.error):
        return None


def store_data(dev, data: bytes, slot: int = 1, progress=None) -> dict:
    """The one path that writes a custom-animation slot: upload, keep a copy, return the parsed file.
    Slot 0 (brightness + 8) is written only by callers that pass it on purpose."""
    Uploader(dev.hidpp).upload(slot, data, progress, allow_slot0=slot == 0)
    save_local(dev.key, slot, data)
    return parse_file(data)


def store_design(dev, design, slot: int = 1, progress=None) -> tuple[dict, int]:
    """Fit a lighting design (smoothest frame rate, most detail) and store it. Returns (file, frame ms)."""
    from ..design import fit_best

    _frames, period, data, _detail = fit_best(design, dev.per_key.zones)
    return store_data(dev, data, slot, progress), period


def store_lighting(dev, light: dict, name: str, slot: int = 1, progress=None) -> dict:
    """Store a profile's lighting: its design, or (older profiles) its painted keys and colour sequence."""
    if light.get("design"):
        from ..design import Design

        return store_design(dev, Design.from_dict({**light["design"], "name": name}), slot, progress)[0]
    return store_data(dev, build_file(name, frames_from_lighting(light, dev.per_key.zones)), slot, progress)
