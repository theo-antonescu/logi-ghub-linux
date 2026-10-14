"""What the user is holding down right now, for scripts' IsModifierPressed,
IsMouseButtonPressed and IsKeyLockOn.

* Lock keys come from the keyboard LEDs in ``/sys/class/leds``, which anyone
  can read.
* Keys and buttons come from ``EVIOCGKEY`` on the ``/dev/input/event*`` nodes
  this user can open. By default that is none (they belong to the ``input``
  group); the daemon still knows what it typed itself and which Logitech mouse
  buttons are down from Mouse Button Spy.
"""

from __future__ import annotations

import fcntl
import glob
import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

_KEY_MAX = 0x2FF
_KEY_BYTES = _KEY_MAX // 8 + 1
EVIOCGKEY = 0x80000000 | (_KEY_BYTES << 16) | (ord("E") << 8) | 0x18

KEY_LEFTCTRL, KEY_A, BTN_LEFT = 29, 30, 0x110


def lock_on(name: str, leds_dir: str = "/sys/class/leds") -> bool:
    """True if any keyboard shows its caps/num/scroll lock LED."""
    for path in glob.glob(os.path.join(leds_dir, f"*::{name.lower()}", "brightness")):
        try:
            with open(path) as f:
                if int(f.read().strip() or 0) > 0:
                    return True
        except (OSError, ValueError):
            continue
    return False


def _input_devices(path: str = "/proc/bus/input/devices") -> list[tuple[str, int]]:
    """(event node, key bitmap as int) for every input device with keys."""
    try:
        text = open(path).read()
    except OSError:
        return []
    out = []
    for block in text.split("\n\n"):
        handler = re.search(r"H: Handlers=.*\b(event\d+)\b", block)
        bits = re.search(r"B: KEY=([0-9a-f ]+)", block)
        if not handler or not bits:
            continue
        words = bits.group(1).split()
        bitmap = 0
        for w in words:  # most significant word first, one per unsigned long
            bitmap = (bitmap << 64) | int(w, 16)
        out.append((f"/dev/input/{handler.group(1)}", bitmap))
    return out


class InputState:
    """Reads key and button state from the input devices this user may open."""

    REFRESH = 5.0

    def __init__(self):
        self._fds: dict[str, int] = {}
        self._checked = 0.0
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        if time.monotonic() - self._checked < self.REFRESH:
            return
        self._checked = time.monotonic()
        wanted = {node for node, bits in _input_devices()
                  if bits >> KEY_LEFTCTRL & 1 and bits >> KEY_A & 1 or bits >> BTN_LEFT & 1}
        for node in list(self._fds):
            if node not in wanted:
                os.close(self._fds.pop(node))
        for node in wanted - set(self._fds):
            try:
                self._fds[node] = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError:
                continue  # not readable by this user: normal without the input group

    def pressed(self, codes: tuple[int, ...] | list[int]) -> bool:
        """True if any of the evdev ``codes`` is held on a readable device."""
        with self._lock:
            self._refresh()
            for node, fd in list(self._fds.items()):
                buf = bytearray(_KEY_BYTES)
                try:
                    fcntl.ioctl(fd, EVIOCGKEY, buf)
                except OSError:
                    os.close(fd)
                    del self._fds[node]
                    continue
                if any(buf[c // 8] >> (c % 8) & 1 for c in codes):
                    return True
        return False

    @property
    def readable(self) -> int:
        with self._lock:
            self._refresh()
            return len(self._fds)

    def close(self) -> None:
        with self._lock:
            for fd in self._fds.values():
                os.close(fd)
            self._fds.clear()
