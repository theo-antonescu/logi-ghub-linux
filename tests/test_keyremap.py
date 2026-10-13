import os
import time

import pytest

from logi_ghub import keyremap, profiles
from logi_ghub.actions import Action, parse_action
from logi_ghub.daemon import MacroRunner
from logi_ghub.macros import Macro

CAPS, A, C, F1, ESC, BACKSPACE, ENTER, LEFTCTRL, LEFTSHIFT, T, MUTE = 58, 30, 46, 59, 1, 14, 28, 29, 42, 20, 113


class Out:
    def __init__(self):
        self.events = []

    def key(self, code, down):
        self.events.append((code, down))

    def wheel(self, clicks, horizontal=False):
        self.events.append(("wheel", clicks, horizontal))


def mapper(assign, macros=None, runner=None):
    kbd, ptr = Out(), Out()
    m = keyremap.KeyMapper(keyremap.resolve(assign), kbd, ptr, runner, {x.name: x for x in macros or []})
    return m, kbd, ptr


def tap(m, code):
    m.handle(code, 1)
    m.handle(code, 0)


# --- key names and sysfs ----------------------------------------------------------------
def test_key_names():
    assert keyremap.key_code("CapsLock") == CAPS
    assert keyremap.key_code("Mute") == MUTE
    assert keyremap.key_code("Previous") == keyremap.key_code("Previous track") == 165
    assert keyremap.key_code("code:0x2a0") == 0x2A0
    assert keyremap.canonical("Next track") == "Next"
    with pytest.raises(ValueError):
        keyremap.key_code("Logo")


def to_bitmap(bits):
    words = [0] * (max(bits) // keyremap.LONG_BITS + 1) if bits else [0]
    for b in bits:
        words[b // keyremap.LONG_BITS] |= 1 << (b % keyremap.LONG_BITS)
    return " ".join("%x" % w for w in reversed(words))


def test_parse_bitmap_round_trip():
    bits = {1, 14, 28, 30, 58, 113, 164, 0x110, 0x2A0}
    assert keyremap.parse_bitmap(to_bitmap(bits)) == bits
    assert keyremap.parse_bitmap("0") == set()


def _input(sysroot, parent, input_name, event, name, key_bits, led_bits=()):
    d = parent / "input" / input_name
    (d / "capabilities").mkdir(parents=True)
    (d / "name").write_text(name + "\n")
    (d / "capabilities" / "key").write_text(to_bitmap(set(key_bits)) + "\n")
    (d / "capabilities" / "led").write_text(to_bitmap(set(led_bits)) + "\n")
    (d / event).mkdir()
    (sysroot / "class" / "input").mkdir(parents=True, exist_ok=True)
    (sysroot / "class" / "input" / event).symlink_to(d / event)


def _hidraw(sysroot, hid_dev, name):
    d = hid_dev / "hidraw" / name
    d.mkdir(parents=True)
    (d / "device").symlink_to(hid_dev)
    (sysroot / "class" / "hidraw").mkdir(parents=True, exist_ok=True)
    (sysroot / "class" / "hidraw" / name).symlink_to(d)


def test_find_keyboard_behind_receiver(tmp_path):
    receiver = tmp_path / "devices" / "usb1" / "1-2" / "1-2:1.2" / "0003:046D:C547.0005"
    keyboard = receiver / "0003:046D:40B6.0006"
    mouse = receiver / "0003:046D:409F.0007"
    _hidraw(tmp_path, keyboard, "hidraw3")
    _input(tmp_path, keyboard, "input20", "event7", "Logitech G915 X LS TKL", range(1, 120), led_bits=[0, 1, 2])
    _input(tmp_path, mouse, "input21", "event8", "Logitech G502 X", [0x110, 0x111, 0x112])
    nodes = keyremap.find_event_nodes("/dev/hidraw3", True, tmp_path / "class" / "hidraw", tmp_path / "class" / "input")
    assert [n.path for n in nodes] == ["/dev/input/event7"]
    assert nodes[0].leds == {0, 1, 2} and CAPS in nodes[0].keys


def test_find_keyboard_over_usb_cable(tmp_path):
    usb = tmp_path / "devices" / "usb1" / "1-3"
    keys_if = usb / "1-3:1.0" / "0003:046D:C35A.0001"
    media_if = usb / "1-3:1.1" / "0003:046D:C35A.0002"
    hidpp_if = usb / "1-3:1.2" / "0003:046D:C35A.0003"
    _hidraw(tmp_path, hidpp_if, "hidraw5")
    _input(tmp_path, keys_if, "input4", "event4", "Logitech G915 X Keyboard", range(1, 120))
    _input(tmp_path, media_if, "input5", "event5", "Logitech G915 X Consumer Control", [MUTE, 114, 115, 163, 164, 165])
    _input(tmp_path, tmp_path / "devices" / "usb1" / "1-4", "input9", "event9", "Other keyboard", range(1, 120))
    nodes = keyremap.find_event_nodes("/dev/hidraw5", False, tmp_path / "class" / "hidraw", tmp_path / "class" / "input")
    assert [n.path for n in nodes] == ["/dev/input/event4", "/dev/input/event5"]


# --- the mapping ------------------------------------------------------------------------
def test_unassigned_keys_pass_through():
    m, kbd, _ = mapper({"CapsLock": Action("default")})
    tap(m, A)
    m.handle(A, 1)
    m.handle(A, 2)  # auto-repeat is left to the virtual keyboard
    m.handle(A, 0)
    assert kbd.events == [(A, True), (A, False), (A, True), (A, False)]


def test_caps_lock_as_ctrl_combines_with_other_keys():
    m, kbd, _ = mapper({"CapsLock": parse_action("key:LeftCtrl")})
    m.handle(CAPS, 1)
    tap(m, C)
    m.handle(CAPS, 0)
    assert kbd.events == [(LEFTCTRL, True), (C, True), (C, False), (LEFTCTRL, False)]


def test_chord_media_and_disabled_keys():
    m, kbd, _ = mapper({"F1": parse_action("key:Ctrl+Shift+T"), "Pause": parse_action("consumer:Mute"),
                        "Mute": Action("none")})
    tap(m, F1)
    tap(m, 119)  # Pause
    tap(m, MUTE)  # disabled: swallowed
    assert kbd.events == [(LEFTCTRL, True), (LEFTSHIFT, True), (T, True), (T, False), (LEFTSHIFT, False), (LEFTCTRL, False),
                          (MUTE, True), (MUTE, False)]


def test_release_uses_the_action_from_press_time():
    m, kbd, _ = mapper({"F1": parse_action("key:A")})
    m.handle(F1, 1)
    m.mapping = {}  # profile changed while the key was down
    m.handle(F1, 0)
    assert kbd.events == [(A, True), (A, False)]


def test_mouse_and_scroll_actions_go_to_the_pointer():
    m, kbd, ptr = mapper({"F1": parse_action("mouse:4"), "F2": parse_action("special:Scroll up")})
    tap(m, F1)
    tap(m, 60)
    assert kbd.events == []
    assert ptr.events == [(0x113, True), (0x113, False), ("wheel", 1, False)]


def test_macro_on_a_key_repeats_while_held():
    kbd_out = Out()
    macro = Macro.parse("spam", "A", repeat="while_held", interval_ms=50)
    m, kbd, _ = mapper({"F1": Action.run_macro("spam")}, [macro], MacroRunner(kbd_out))
    m.handle(F1, 1)
    time.sleep(0.18)
    m.handle(F1, 0)
    time.sleep(0.1)
    assert kbd.events == []
    presses = [e for e in kbd_out.events if e == (A, True)]
    assert 2 <= len(presses) <= 5 and kbd_out.events[-1] == (A, False)


def test_escape_keys_panic():
    m, kbd, _ = mapper({"Esc": parse_action("key:CapsLock")})
    m.handle(BACKSPACE, 1)
    m.handle(ESC, 1)
    assert not m.panicked
    m.handle(ENTER, 1)
    assert m.panicked
    m.release_all()
    assert set(kbd.events) == {(BACKSPACE, True), (CAPS, True), (BACKSPACE, False), (CAPS, False)}


def test_release_all_lets_go_of_everything():
    m, kbd, _ = mapper({"CapsLock": parse_action("key:LeftCtrl")})
    m.handle(CAPS, 1)
    m.handle(A, 1)
    m.release_all()
    assert kbd.events == [(LEFTCTRL, True), (A, True), (LEFTCTRL, False), (A, False)]


def test_profile_round_trip_keeps_key_actions():
    p = profiles.Profile(name="kb", device="046d:40b6", key_actions={"CapsLock": parse_action("key:LeftCtrl")})
    back = profiles.Profile.from_dict(p.to_dict())
    assert back.key_actions == p.key_actions
    assert profiles.Profile.from_dict({"name": "old", "device": "046d:40b6"}).key_actions == {}


# --- the reader thread, with FIFOs standing in for /dev/input/event* -----------------------
class FakeVirtual(Out):
    def __init__(self, key_codes, leds):
        super().__init__()
        self.key_codes, self.leds = key_codes, leds
        self.fd = -1
        self.closed = False

    def close(self):
        self.closed = True


class GrablessRemapper(keyremap.KeyRemapper):
    grabbed: dict

    @staticmethod
    def _any_key_down(fd):
        return False

    def _grab(self, fd, on):
        self.grabbed[fd] = on


def test_remapper_reads_events_and_lets_go(tmp_path):
    fifo = tmp_path / "event7"
    os.mkfifo(fifo)
    node = keyremap.EventNode(str(fifo), "kb", keys={A, CAPS, C})
    made = []
    r = GrablessRemapper("kb", [node], keyremap.resolve({"CapsLock": parse_action("key:LeftCtrl")}),
                     virtual_factory=lambda k, l: made.append(FakeVirtual(k, l)) or made[-1])
    r.grabbed = {}
    r.start()
    v = made[0]
    assert LEFTCTRL in v.key_codes and A in v.key_codes
    assert list(r.grabbed.values()) == [True]
    w = os.open(fifo, os.O_WRONLY)
    ev = keyremap.EVENT
    os.write(w, ev.pack(0, 0, 1, CAPS, 1) + ev.pack(0, 0, 4, 4, 0x39) + ev.pack(0, 0, 0, 0, 0)
             + ev.pack(0, 0, 1, C, 1) + ev.pack(0, 0, 1, C, 0) + ev.pack(0, 0, 1, CAPS, 0))
    deadline = time.time() + 2
    while len(v.events) < 4 and time.time() < deadline:
        time.sleep(0.01)
    r.stop()
    os.close(w)
    assert v.events == [(LEFTCTRL, True), (C, True), (C, False), (LEFTCTRL, False)]
    assert list(r.grabbed.values()) == [False] and v.closed and not r.running
