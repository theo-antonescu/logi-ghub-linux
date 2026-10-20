"""Virtual G502 X LIGHTSPEED and G915 X LIGHTSPEED TKL, as if plugged in by USB cable.

The HID++ 2.0 answers come from the simulated models in ``hidpp.fake`` (the same ones ``--fake``
uses), so the app, Solaar or G HUB can query and configure them. On top of that each device sends
ordinary HID input reports, driven from Python::

    with VirtualMouse() as mouse, VirtualKeyboard() as kb:
        mouse.move(100, 0)
        mouse.click("left")
        kb.tap("LeftCtrl", "C")
"""

from __future__ import annotations

import struct
import threading
import time

from .. import keys
from ..hidpp import fake
from ..hidpp.fake import FakeError
from ..hidpp.transport import LOGITECH_VID, REPORT_LONG, REPORT_SHORT
from .backends import BACKENDS

ERR_INVALID_ARGUMENT = 0x02
HIDPP10_ERROR = 0x8F

# HID report descriptors. Report ids 1-3 are input; 0x10/0x11 are the HID++ short and long reports
# in the vendor page that every Logitech HID++ device exposes.
KEYBOARD_DESC = bytes.fromhex(
    "05 01 09 06 a1 01 85 01 05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02 95 01 75 08 81 01"
    "95 06 75 08 15 00 26 ff 00 05 07 19 00 2a ff 00 81 00 c0"
)
CONSUMER_DESC = bytes.fromhex("05 0c 09 01 a1 01 85 03 75 10 95 01 15 00 26 ff 03 19 00 2a ff 03 81 00 c0")
MOUSE_DESC = bytes.fromhex(
    "05 01 09 02 a1 01 85 02 09 01 a1 00 05 09 19 01 29 10 15 00 25 01 95 10 75 01 81 02"  # 16 buttons
    "05 01 16 01 80 26 ff 7f 75 10 95 02 09 30 09 31 81 06"  # X, Y
    "15 81 25 7f 75 08 95 01 09 38 81 06 05 0c 0a 38 02 95 01 81 06 c0 c0"  # wheel, horizontal wheel
)
HIDPP_DESC = bytes.fromhex(
    "06 00 ff 09 01 a1 01 85 10 75 08 95 06 15 00 26 ff 00 09 01 81 00 09 01 91 00 c0"
    "06 00 ff 09 02 a1 01 85 11 75 08 95 13 15 00 26 ff 00 09 02 81 00 09 02 91 00 c0"
)


class Identity:
    """Firmware and model ids (0x0003) as the real device reports them, from data/devices/*.json."""

    usb_pid = 0
    firmware: list[tuple[int, str, int, int, int]] = []  # (type, name, major, minor, build)

    def f_0003(self, fn, p):
        if fn == 0:
            unit, transports = 0x12345678, 0x000C  # Lightspeed + USB
            ids = struct.pack(">HHH", int(self.key[5:], 16), self.usb_pid, 0)
            return bytes([len(self.firmware)]) + struct.pack(">IH", unit, transports) + ids + b"\x00\x00"
        if fn == 1:
            kind, name, major, minor, build = self.firmware[p[0]]
            return bytes([kind]) + name.encode() + struct.pack(">BBH", major, minor, build)
        raise FakeError(0x07)


class G502XModel(Identity, fake.FakeG502X):
    usb_pid = 0xC098  # check with `lsusb` on the real mouse plugged in by cable
    firmware = [(0, "MPM", 0x30, 0x00, 0x0014), (1, "BL1", 0x48, 0x00, 0x0014)]


class G915XModel(Identity, fake.FakeKeyboard):
    name = "G915 X LS TKL"
    usb_pid = 0xC357  # from tools/ghub-capture (the keyboard on a cable)
    firmware = [(0, "MPK", 0x27, 0x00, 0x0012), (1, "BL1", 0x85, 0x00, 0x0012)]


class VirtualDevice:
    """A HID++ device on a backend: answers HID++ requests and sends input reports."""

    descriptor = HIDPP_DESC

    def __init__(self, backend: str = "uhid", model: fake.FakeModel | None = None, **backend_args):
        self.model = model or self.Model()
        self.lock = threading.Lock()
        self.backend = BACKENDS[backend](self.model.name, LOGITECH_VID, self.model.usb_pid,
                                         self.descriptor, **backend_args)
        self.backend.start(self.on_output)

    def on_output(self, data: bytes) -> None:
        if len(data) < 4 or data[0] not in (REPORT_SHORT, REPORT_LONG):
            return
        dev, sub_id, address = data[1:4]
        if sub_id >= 0x80:  # HID++ 1.0 register access: a corded HID++ 2.0 device has none
            return self.send(bytes([REPORT_SHORT, dev, HIDPP10_ERROR, sub_id, address, 0x01, 0]))
        try:
            reply = self.model.handle(sub_id, address >> 4, bytes(data[4:]).ljust(16, b"\x00"))
            self.send(bytes([REPORT_LONG, dev, sub_id, address]) + reply.ljust(16, b"\x00")[:16])
        except (FakeError, IndexError, KeyError, TypeError, struct.error) as e:
            code = e.code if isinstance(e, FakeError) else ERR_INVALID_ARGUMENT
            self.send(bytes([REPORT_LONG, dev, 0xFF, sub_id, address, code]).ljust(20, b"\x00"))

    def notify(self, feature_id: int, function: int, data: bytes) -> None:
        """Send an unsolicited HID++ event (software id 0) from ``feature_id``."""
        self.send(bytes([REPORT_LONG, 0xFF, self.model.index(feature_id), function << 4]) + data.ljust(16, b"\x00"))

    def send(self, report: bytes) -> None:
        with self.lock:
            self.backend.send(report)

    def close(self) -> None:
        self.backend.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class VirtualMouse(VirtualDevice):
    Model = G502XModel
    descriptor = MOUSE_DESC + HIDPP_DESC
    BUTTONS = {"left": 1, "right": 2, "middle": 3, "back": 4, "forward": 5}

    def __init__(self, *args, **kwargs):
        self.down: set[int] = set()
        super().__init__(*args, **kwargs)

    def move(self, dx: int, dy: int) -> None:
        self._report(dx=dx, dy=dy)

    def scroll(self, clicks: int = 0, horizontal: int = 0) -> None:
        self._report(wheel=clicks, pan=horizontal)

    def press(self, button: str | int) -> None:
        self.down.add(self.BUTTONS.get(button, button))
        self._buttons_changed()

    def release(self, button: str | int) -> None:
        self.down.discard(self.BUTTONS.get(button, button))
        self._buttons_changed()

    def click(self, button: str | int = "left", hold: float = 0.02) -> None:
        self.press(button)
        time.sleep(hold)
        self.release(button)

    def _buttons_changed(self) -> None:
        if self.model.spying:  # Mouse Button Spy: report physical buttons to the host software
            mask = sum(1 << (b - 1) for b in self.down)
            self.notify(fake.F.MOUSE_BUTTON_SPY, 0, struct.pack(">H", mask))
        self._report()

    def _report(self, dx=0, dy=0, wheel=0, pan=0) -> None:
        # with spying on, the mouse sends each button as the host remapped it (0 = only to the software)
        remap = self.model.remap if self.model.spying else range(1, 17)
        hid = {remap[b - 1] for b in self.down if b <= len(remap)} - {0}
        clamp = lambda v, n: max(-n, min(n, v))
        self.send(struct.pack("<BHhhbb", 2, sum(1 << (b - 1) for b in hid),
                              clamp(dx, 32767), clamp(dy, 32767), clamp(wheel, 127), clamp(pan, 127)))


class VirtualKeyboard(VirtualDevice):
    Model = G915XModel
    descriptor = KEYBOARD_DESC + CONSUMER_DESC + HIDPP_DESC

    def __init__(self, *args, **kwargs):
        self.down: list[str] = []
        super().__init__(*args, **kwargs)

    def key_down(self, *names: str) -> None:
        self.down += [n for n in names if n not in self.down]
        self._report(names)

    def key_up(self, *names: str) -> None:
        self.down = [n for n in self.down if n not in names]
        self._report(names)

    def tap(self, *names: str, hold: float = 0.02) -> None:
        """Press the keys in order, then release them: ``tap("LeftCtrl", "C")``."""
        self.key_down(*names)
        time.sleep(hold)
        self.key_up(*reversed(names))

    def _report(self, changed) -> None:
        if any(n in keys.CONSUMER_BY_NAME for n in changed):
            media = [keys.CONSUMER_BY_NAME[n][0] for n in self.down if n in keys.CONSUMER_BY_NAME]
            self.send(struct.pack("<BH", 3, media[-1] if media else 0))
        if any(n not in keys.CONSUMER_BY_NAME for n in changed):
            mods = keys.modifier_mask([n for n in self.down if n in keys.MODIFIERS])
            usages = [keys.hid_for(n) for n in self.down if n not in keys.MODIFIERS and n not in keys.CONSUMER_BY_NAME]
            self.send(bytes([1, mods, 0]) + bytes(usages[:6]).ljust(6, b"\x00"))
