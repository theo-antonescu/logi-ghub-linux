"""Host-mode key remapping for keyboards.

The G915 X has no G-keys and stores its key assignments through an
undocumented feature (0x1B05), so instead of changing the keyboard this works
like keyd or G HUB's host mode: while the daemon runs a profile with key
assignments it takes the keyboard's input device for itself (EVIOCGRAB),
passes every key through a virtual keyboard unchanged, and performs the
profile's action for the keys that have one. Nothing is written to the
keyboard. When the daemon stops, the grab ends and the keyboard types
normally again.

The same grab also runs the double-press filter (chatter.py), which can be
on without any key assignments.

Safety: holding Backspace + Esc + Enter together always releases the keyboard.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import select
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import keys
from .actions import SPECIALS, Action

log = logging.getLogger(__name__)

EVIOCGRAB = 0x40044590
EVIOCGKEY_96 = 0x80604518  # _IOR('E', 0x18, char[96]): which keys are down (KEY_MAX = 0x2ff)
EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit Linux
LONG_BITS = struct.calcsize("l") * 8
SYS_INPUT = Path("/sys/class/input")
SYS_HIDRAW = Path("/sys/class/hidraw")

KEY_ESC, KEY_BACKSPACE, KEY_ENTER = 1, 14, 28
PANIC = frozenset({KEY_BACKSPACE, KEY_ESC, KEY_ENTER})

# Keys of the G915 X's media row, by the names the on-screen keyboard uses.
MEDIA_KEYS = {"Mute": "Mute", "Play/Pause": "Play/Pause", "Previous": "Previous track", "Next": "Next track"}


# --- key names ---------------------------------------------------------------------
def key_code(name: str) -> int:
    """evdev code for a key name: keys.KEY_NAMES, a media key (keys.CONSUMER_NAMES) or ``code:N``."""
    name = MEDIA_KEYS.get(name, name)
    if name in keys.KEY_BY_NAME:
        return keys.KEY_BY_NAME[name][1]
    if name in keys.CONSUMER_BY_NAME:
        return keys.CONSUMER_BY_NAME[name][1]
    if name.lower().startswith("code:"):
        return int(name[5:], 0)
    raise ValueError(f"unknown key {name!r}; use a key name like CapsLock or F1, a media key like Mute, or code:N")


# --- finding the keyboard's input devices ---------------------------------------------
def canonical(name: str) -> str:
    """The name a key is stored under: media keys use the on-screen keyboard's names."""
    for short, long in MEDIA_KEYS.items():
        if name == long:
            return short
    return name


def parse_bitmap(text: str) -> set[int]:
    """Decode a sysfs capability bitmap ("120013 803078f900d401 ..."), most significant word first."""
    bits = set()
    for word_index, word in enumerate(reversed(text.split())):
        value = int(word, 16)
        base = word_index * LONG_BITS
        while value:
            low = value & -value
            bits.add(base + low.bit_length() - 1)
            value ^= low
    return bits


@dataclass
class EventNode:
    path: str  # /dev/input/eventN
    name: str
    keys: set[int] = field(default_factory=set)
    leds: set[int] = field(default_factory=set)

    @property
    def is_keyboard(self) -> bool:
        consumer = set(keys.CONSUMER_TO_EVDEV.values())
        return any(1 <= k < 0x100 or k in consumer for k in self.keys)


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _nodes_under(base: Path, sys_input: Path, dev_root: str) -> list[EventNode]:
    found = []
    if not sys_input.exists():
        return found
    for entry in sorted(sys_input.glob("event*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0):
        real = entry.resolve()
        if base != real and base not in real.parents:
            continue
        caps = real.parent / "capabilities"
        node = EventNode(
            path=f"{dev_root}/{entry.name}",
            name=_read(real.parent / "name"),
            keys=parse_bitmap(_read(caps / "key") or "0"),
            leds=parse_bitmap(_read(caps / "led") or "0"),
        )
        if node.is_keyboard:
            found.append(node)
    return found


def find_event_nodes(hidraw_path: str, behind_receiver: bool, sys_hidraw: Path = SYS_HIDRAW,
                     sys_input: Path = SYS_INPUT, dev_root: str = "/dev/input") -> list[EventNode]:
    """The keyboard input devices that belong to the device on ``hidraw_path``.

    Behind a receiver the logitech-hidpp driver puts the input device next to
    the device's hidraw node. Over a USB cable, keys arrive on another USB
    interface than HID++, so look at the whole USB device.
    """
    hid_dev = (sys_hidraw / os.path.basename(hidraw_path) / "device").resolve()
    nodes = _nodes_under(hid_dev, sys_input, dev_root)
    if not nodes and not behind_receiver:
        usb_device = hid_dev.parent.parent  # HID device -> USB interface -> USB device
        nodes = _nodes_under(usb_device, sys_input, dev_root)
    return nodes


# --- the remapping itself ----------------------------------------------------------------
def resolve(key_actions: dict[str, Action]) -> dict[int, Action]:
    """Profile key assignments by evdev code; keys left at "default" type normally."""
    out = {}
    for name, action in key_actions.items():
        if action.kind == "default":
            continue
        try:
            out[key_code(name)] = action
        except ValueError as e:
            log.warning("%s", e)
    return out


def action_key_codes(action: Action) -> list[int]:
    """Keys a key/media-key action presses on the virtual keyboard, in press order."""
    if action.kind == "key":
        codes = [keys.KEY_BY_NAME[keys.MODIFIERS[b]][1] for b in range(8) if action.modifiers & (1 << b)]
        if action.key in keys.HID_TO_EVDEV:
            codes.append(keys.HID_TO_EVDEV[action.key])
        return codes
    if action.kind == "consumer":
        code = keys.CONSUMER_TO_EVDEV.get(action.usage)
        return [code] if code else []
    return []


class KeyMapper:
    """Turns physical key events into virtual ones. No I/O of its own, so it can be tested.

    ``keyboard`` and ``pointer`` need ``key(code, down)``; ``pointer`` also
    ``wheel(clicks, horizontal)``. ``runner`` plays macros (daemon.MacroRunner).
    """

    def __init__(self, mapping: dict[int, Action], keyboard, pointer=None, runner=None, macros=None, tag: str = "keys"):
        self.mapping = mapping
        self.keyboard = keyboard
        self.pointer = pointer
        self.runner = runner
        self.macros = macros or {}
        self.tag = tag
        self.physical_down: set[int] = set()
        self.passed: set[int] = set()  # keys passed through and still held
        self.held: dict[int, tuple[Action, list[int]]] = {}  # remapped keys still held -> what was pressed
        self.panicked = False

    def handle(self, code: int, value: int) -> None:
        """value: 1 press, 0 release, 2 auto-repeat (ignored: the virtual keyboard repeats by itself)."""
        if value == 2:
            return
        if value:
            self.physical_down.add(code)
            if PANIC <= self.physical_down:
                self.panicked = True
                return
        else:
            self.physical_down.discard(code)
        if value:
            action = self.mapping.get(code)
            if action is None:
                self.keyboard.key(code, True)
                self.passed.add(code)
            else:
                self.held[code] = (action, self._press(code, action))
        else:
            if code in self.held:
                action, codes = self.held.pop(code)
                self._release(code, action, codes)
            elif code in self.passed:
                self.passed.discard(code)
                self.keyboard.key(code, False)

    def _press(self, code: int, a: Action) -> list[int]:
        if a.kind in ("key", "consumer"):
            codes = action_key_codes(a)
            for c in codes:
                self.keyboard.key(c, True)
            return codes
        if a.kind == "mouse" and self.pointer is not None:
            btn = keys.MOUSE_BUTTONS.get(a.button, ("", 0x110))[1]
            self.pointer.key(btn, True)
            return [btn]
        if a.kind == "macro" and self.runner is not None:
            macro = self.macros.get(a.macro)
            if macro is not None:
                self.runner.press(f"{self.tag}:{code}", macro)
            else:
                log.warning("key %d: unknown macro %r", code, a.macro)
            return []
        if a.kind == "special":
            f = a.function
            if f in (0x01, 0x02, 0x10, 0x11) and self.pointer is not None:
                self.pointer.wheel(-1 if f in (0x01, 0x10) else 1, f in (0x01, 0x02))
            else:
                log.info("key %d: %s does nothing on a keyboard", code, SPECIALS.get(f, f"function {f:#x}"))
        return []  # "none": the key is swallowed

    def _release(self, code: int, a: Action, codes: list[int]) -> None:
        target = self.pointer if a.kind == "mouse" else self.keyboard
        for c in reversed(codes):
            target.key(c, False)
        if a.kind == "macro" and self.runner is not None:
            macro = self.macros.get(a.macro)
            if macro is not None:
                self.runner.release(f"{self.tag}:{code}", macro)

    def release_all(self) -> None:
        for code in list(self.held):
            action, codes = self.held.pop(code)
            self._release(code, action, codes)
        for code in sorted(self.passed):
            self.keyboard.key(code, False)
        self.passed.clear()
        self.physical_down.clear()


class KeyRemapper:
    """Grabs a keyboard's input devices and feeds them through a KeyMapper on its own thread.

    UNTESTED ON HARDWARE: written without access to the G915 X. The logic is
    covered by tests with fake input devices; grabbing real ones has not been
    tried yet.
    """

    def __init__(self, name: str, nodes: list[EventNode], mapping: dict[int, Action], pointer=None, runner=None,
                 macros=None, tag: str = "keys", virtual_factory=None, chatter=None):
        self.name = name
        self.chatter = chatter  # chatter.ChatterFilter: drops double presses before the mapper sees them
        self.nodes = nodes
        self.mapping = mapping
        self.pointer = pointer
        self.runner = runner
        self.macros = macros or {}
        self.tag = tag
        self.virtual_factory = virtual_factory or self._make_virtual
        self.fds: dict[int, EventNode] = {}
        self.virtual = None
        self.mapper: KeyMapper | None = None
        self.thread: threading.Thread | None = None
        self.lost = False
        self.panicked = False
        self._wake_r, self._wake_w = -1, -1
        self._stopping = threading.Event()
        self._close_lock = threading.Lock()

    def _make_virtual(self, key_codes: set[int], leds: set[int]):
        from .uinput import VirtualInput

        return VirtualInput(f"{self.name} (logi-ghub-linux)", key_codes=key_codes, pointer=False, leds=leds,
                            repeat=True, product=0x0FFE)

    def start(self) -> None:
        """Open and grab every node, then start reading. Raises OSError (nothing stays grabbed) on failure."""
        try:
            for node in self.nodes:
                fd = os.open(node.path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
                self.fds[fd] = node
            key_codes = set().union(*(n.keys for n in self.nodes))
            for a in self.mapping.values():
                key_codes.update(action_key_codes(a))
            leds = set().union(*(n.leds for n in self.nodes))
            self.virtual = self.virtual_factory(key_codes, leds)
            self._wait_for_keys_up()
            for fd in self.fds:
                self._grab(fd, True)
        except OSError:
            self._close()
            raise
        self.mapper = KeyMapper(self.mapping, self.virtual, self.pointer, self.runner, self.macros, self.tag)
        self._wake_r, self._wake_w = os.pipe()
        self.thread = threading.Thread(target=self._run, name=f"keys-{self.name}", daemon=True)
        self.thread.start()
        doing = [f"{len(self.mapping)} key assignment(s)"] if self.mapping else []
        if self.chatter is not None:
            doing.append("double-press filter")
        log.info("%s: %s active (%s)", self.name, " and ".join(doing) or "pass-through",
                 ", ".join(n.path for n in self.nodes))

    def _wait_for_keys_up(self, timeout: float = 3.0) -> None:
        """Grabbing while a key is down would leave the desktop thinking it is still held."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if not any(self._any_key_down(fd) for fd in self.fds):
                    return
            except OSError:
                return
            time.sleep(0.05)

    @staticmethod
    def _grab(fd: int, on: bool) -> None:
        fcntl.ioctl(fd, EVIOCGRAB, 1 if on else 0)

    @staticmethod
    def _any_key_down(fd: int) -> bool:
        buf = bytearray(96)
        fcntl.ioctl(fd, EVIOCGKEY_96, buf)
        return any(buf)

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _run(self) -> None:
        virtual_fd = getattr(self.virtual, "fd", -1)
        watch = list(self.fds) + [self._wake_r] + ([virtual_fd] if virtual_fd >= 0 else [])
        try:
            while not self._stopping.is_set():
                ready, _, _ = select.select(watch, [], [])
                for fd in ready:
                    if fd == self._wake_r:
                        return
                    if fd == virtual_fd:
                        self._forward_leds(fd)
                    else:
                        self._read_keys(fd)
                    if self.mapper.panicked:
                        log.warning("%s: Backspace+Esc+Enter held, letting go of the keyboard", self.name)
                        self.panicked = True
                        return
        except OSError as e:
            if e.errno not in (errno.ENODEV, errno.EBADF):
                log.warning("%s: key remapping stopped: %s", self.name, e)
            self.lost = True
        finally:
            self._close()

    def _read_keys(self, fd: int) -> None:
        try:
            data = os.read(fd, EVENT.size * 64)
        except BlockingIOError:
            return
        if not data:
            raise OSError(errno.ENODEV, "input device gone")
        for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
            sec, usec, etype, code, value = EVENT.unpack_from(data, off)
            if etype != keys.EV_KEY:
                continue
            if self.chatter is not None and not self.chatter.allow(code, value, sec + usec / 1e6):
                continue
            self.mapper.handle(code, value)

    def _forward_leds(self, fd: int) -> None:
        """The desktop sets Caps/Num Lock LEDs on the virtual keyboard; pass that on to the real one."""
        try:
            data = os.read(fd, EVENT.size * 16)
        except BlockingIOError:
            return
        for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _s, _us, etype, code, value = EVENT.unpack_from(data, off)
            if etype != keys.EV_LED:
                continue
            for real_fd, node in self.fds.items():
                if code in node.leds:
                    try:
                        os.write(real_fd, EVENT.pack(0, 0, keys.EV_LED, code, value) + EVENT.pack(0, 0, 0, 0, 0))
                    except OSError:
                        pass

    def stop(self) -> None:
        self._stopping.set()
        with self._close_lock:  # the reader thread may be closing the pipe right now
            if self._wake_w >= 0:
                try:
                    os.write(self._wake_w, b"x")
                except OSError:
                    pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self._close()

    def _close(self) -> None:
        with self._close_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self.mapper is not None:
            try:
                self.mapper.release_all()
            except OSError:
                pass
        for fd in list(self.fds):
            try:
                self._grab(fd, False)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()
        if self.virtual is not None:
            try:
                self.virtual.close()
            except OSError:
                pass
            self.virtual = None
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = -1
