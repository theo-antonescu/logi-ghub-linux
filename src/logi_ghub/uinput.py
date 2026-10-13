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
UI_SET_LEDBIT = 0x40045569
BUS_VIRTUAL = 0x06
SYN_REPORT = 0


class VirtualInput:
    """The default device has every key, media key and mouse button the daemon emits, plus wheels.

    The key remapper makes keyboard-only devices instead: ``key_codes`` lists
    the keys, ``leds`` the LEDs the desktop can switch (the device is then
    opened read-write so the remapper can read those LED changes back), and
    ``repeat`` lets the kernel auto-repeat held keys.
    """

    def __init__(self, name: str = "logi-ghub-linux virtual input", path: str = "/dev/uinput", *,
                 key_codes: set[int] | None = None, pointer: bool = True, leds: set[int] = frozenset(),
                 repeat: bool = False, product: int = 0x0FFF):
        self.fd = os.open(path, (os.O_RDWR if leds else os.O_WRONLY) | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, keys.EV_KEY)
            if key_codes is None:
                codes = set(keys.HID_TO_EVDEV.values()) | set(keys.CONSUMER_TO_EVDEV.values())
                codes |= {ev for _, ev in keys.MOUSE_BUTTONS.values()}
            else:
                codes = set(key_codes)
            for code in sorted(codes):
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
            if pointer:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, keys.EV_REL)
                for rel in (keys.REL_X, keys.REL_Y, keys.REL_WHEEL, keys.REL_HWHEEL, keys.REL_WHEEL_HI_RES, keys.REL_HWHEEL_HI_RES):
                    fcntl.ioctl(self.fd, UI_SET_RELBIT, rel)
            if leds:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, keys.EV_LED)
                for led in sorted(leds):
                    fcntl.ioctl(self.fd, UI_SET_LEDBIT, led)
            if repeat:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, keys.EV_REP)
            setup = struct.pack("HHHH80sI", BUS_VIRTUAL, 0x046D, product, 1, name.encode()[:79], 0)
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

    def close(self) -> None:
        if self.fd >= 0:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            finally:
                os.close(self.fd)
                self.fd = -1


def key_code(name: str) -> int:
    return keys.HID_TO_EVDEV[keys.hid_for(name)]
