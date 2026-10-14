"""Virtual keyboard + mouse through /dev/uinput, so the daemon can play the
user's macros and remapped buttons (the same approach Solaar uses).

Plain ioctl calls, so no python-evdev dependency. /dev/uinput needs to be
writable by the user; the bundled udev rule tags it ``uaccess``.
"""

from __future__ import annotations

import fcntl
import os
import struct
import time

from . import keys

UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567
UI_ABS_SETUP = 0x401C5504
EV_ABS, ABS_X, ABS_Y = 0x03, 0x00, 0x01
BUS_VIRTUAL = 0x06
SYN_REPORT = 0


class VirtualInput:
    def __init__(self, name: str = "logi-ghub-linux virtual input", path: str = "/dev/uinput"):
        self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, keys.EV_KEY)
            codes = set(keys.HID_TO_EVDEV.values()) | set(keys.CONSUMER_TO_EVDEV.values())
            codes |= {ev for _, ev in keys.MOUSE_BUTTONS.values()}
            for code in sorted(codes):
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, keys.EV_REL)
            for rel in (keys.REL_X, keys.REL_Y, keys.REL_WHEEL, keys.REL_HWHEEL, keys.REL_WHEEL_HI_RES, keys.REL_HWHEEL_HI_RES):
                fcntl.ioctl(self.fd, UI_SET_RELBIT, rel)
            setup = struct.pack("HHHH80sI", BUS_VIRTUAL, 0x046D, 0x0FFF, 1, name.encode()[:79], 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise
        time.sleep(0.05)  # give the compositor a moment to pick the device up

    def _emit(self, etype: int, code: int, value: int) -> None:
        now = time.time()
        sec = int(now)
        os.write(self.fd, struct.pack("llHHi", sec, int((now - sec) * 1_000_000), etype, code, value))

    def sync(self) -> None:
        self._emit(0, SYN_REPORT, 0)

    def key(self, code: int, down: bool) -> None:
        self._emit(keys.EV_KEY, code, 1 if down else 0)
        self.sync()

    def wheel(self, clicks: int, horizontal: bool = False) -> None:
        self._emit(keys.EV_REL, keys.REL_HWHEEL if horizontal else keys.REL_WHEEL, clicks)
        self._emit(keys.EV_REL, keys.REL_HWHEEL_HI_RES if horizontal else keys.REL_WHEEL_HI_RES, clicks * 120)
        self.sync()

    def move(self, dx: int, dy: int) -> None:
        if dx:
            self._emit(keys.EV_REL, keys.REL_X, dx)
        if dy:
            self._emit(keys.EV_REL, keys.REL_Y, dy)
        self.sync()

    def close(self) -> None:
        if self.fd >= 0:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            finally:
                os.close(self.fd)
                self.fd = -1


class VirtualPointer(VirtualInput):
    """An absolute pointer (like a VM's USB tablet) for scripts' MoveMouseTo.

    Positions run 0-65535 across the whole desktop, as on Windows; the
    compositor maps the device to the area spanning all monitors.
    """

    def __init__(self, name: str = "logi-ghub-linux virtual pointer", path: str = "/dev/uinput"):
        self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, keys.EV_KEY)
            for code in (0x110, 0x111, 0x112):  # buttons make libinput treat it as a pointer
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
            for axis in (ABS_X, ABS_Y):
                fcntl.ioctl(self.fd, UI_SET_ABSBIT, axis)
                # struct uinput_abs_setup: code, then input_absinfo (value, min, max, fuzz, flat, resolution)
                fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("H2x6i", axis, 0, 0, 65535, 0, 0, 0))
            setup = struct.pack("HHHH80sI", BUS_VIRTUAL, 0x046D, 0x0FFE, 1, name.encode()[:79], 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise
        self._created = time.monotonic()
        self._last: tuple[int, int] | None = None

    SETTLE = 1.0  # the compositor needs about a second to start listening to a new input device

    def move_to(self, x: int, y: int) -> None:
        wait = self._created + self.SETTLE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        x, y = max(0, min(65535, int(x))), max(0, min(65535, int(y)))
        if (x, y) == self._last:
            # The kernel drops an axis value equal to the last one, and the real mouse has probably
            # moved the pointer since, so step one unit aside first to make the move happen again.
            self._emit(EV_ABS, ABS_X, x - 1 if x else x + 1)
            self._emit(EV_ABS, ABS_Y, y - 1 if y else y + 1)
            self.sync()
        self._emit(EV_ABS, ABS_X, x)
        self._emit(EV_ABS, ABS_Y, y)
        self.sync()
        self._last = (x, y)


def key_code(name: str) -> int:
    return keys.HID_TO_EVDEV[keys.hid_for(name)]
