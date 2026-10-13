"""Key tables: HID usages (what the device stores) and Linux evdev codes (what uinput emits)."""

from __future__ import annotations

# name -> (HID keyboard usage, evdev code)
_KEYS: list[tuple[str, int, int]] = [
    *[(chr(ord("A") + i), 0x04 + i, code) for i, code in enumerate(
        [30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44]
    )],
    *[(str(d), 0x1E + i, 2 + i) for i, d in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9])],
    ("0", 0x27, 11),
    ("Enter", 0x28, 28),
    ("Esc", 0x29, 1),
    ("Backspace", 0x2A, 14),
    ("Tab", 0x2B, 15),
    ("Space", 0x2C, 57),
    ("Minus", 0x2D, 12),
    ("Equal", 0x2E, 13),
    ("LeftBracket", 0x2F, 26),
    ("RightBracket", 0x30, 27),
    ("Backslash", 0x31, 43),
    ("NonUSHash", 0x32, 43),
    ("Semicolon", 0x33, 39),
    ("Apostrophe", 0x34, 40),
    ("Grave", 0x35, 41),
    ("Comma", 0x36, 51),
    ("Dot", 0x37, 52),
    ("Slash", 0x38, 53),
    ("CapsLock", 0x39, 58),
    *[(f"F{i}", 0x3A + i - 1, code) for i, code in zip(range(1, 13), [59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88])],
    ("PrintScreen", 0x46, 99),
    ("ScrollLock", 0x47, 70),
    ("Pause", 0x48, 119),
    ("Insert", 0x49, 110),
    ("Home", 0x4A, 102),
    ("PageUp", 0x4B, 104),
    ("Delete", 0x4C, 111),
    ("End", 0x4D, 107),
    ("PageDown", 0x4E, 109),
    ("Right", 0x4F, 106),
    ("Left", 0x50, 105),
    ("Down", 0x51, 108),
    ("Up", 0x52, 103),
    ("NumLock", 0x53, 69),
    ("KPSlash", 0x54, 98),
    ("KPAsterisk", 0x55, 55),
    ("KPMinus", 0x56, 74),
    ("KPPlus", 0x57, 78),
    ("KPEnter", 0x58, 96),
    ("KP1", 0x59, 79),
    ("KP2", 0x5A, 80),
    ("KP3", 0x5B, 81),
    ("KP4", 0x5C, 75),
    ("KP5", 0x5D, 76),
    ("KP6", 0x5E, 77),
    ("KP7", 0x5F, 71),
    ("KP8", 0x60, 72),
    ("KP9", 0x61, 73),
    ("KP0", 0x62, 82),
    ("KPDot", 0x63, 83),
    ("NonUSBackslash", 0x64, 86),
    ("Menu", 0x65, 127),
    ("KPEqual", 0x67, 117),
    *[(f"F{i}", 0x68 + i - 13, 183 + i - 13) for i in range(13, 25)],
    ("LeftCtrl", 0xE0, 29),
    ("LeftShift", 0xE1, 42),
    ("LeftAlt", 0xE2, 56),
    ("LeftMeta", 0xE3, 125),
    ("RightCtrl", 0xE4, 97),
    ("RightShift", 0xE5, 54),
    ("RightAlt", 0xE6, 100),
    ("RightMeta", 0xE7, 126),
]

KEY_BY_NAME = {name: (hid, ev) for name, hid, ev in _KEYS}
KEY_NAMES = [name for name, _, _ in _KEYS]
HID_TO_NAME: dict[int, str] = {}
for _name, _hid, _ev in _KEYS:
    HID_TO_NAME.setdefault(_hid, _name)
HID_TO_EVDEV = {hid: ev for _, hid, ev in _KEYS}
EVDEV_TO_NAME: dict[int, str] = {}
for _name, _hid, _ev in _KEYS:
    EVDEV_TO_NAME.setdefault(_ev, _name)

# Modifier bit order in HID++ button bindings and onboard macros.
MODIFIERS = ["LeftCtrl", "LeftShift", "LeftAlt", "LeftMeta", "RightCtrl", "RightShift", "RightAlt", "RightMeta"]
MODIFIER_LABELS = ["Ctrl", "Shift", "Alt", "Super", "RCtrl", "RShift", "AltGr", "RSuper"]

# name -> (HID consumer usage, evdev code)
_CONSUMER: list[tuple[str, int, int]] = [
    ("Play/Pause", 0xCD, 164),
    ("Next track", 0xB5, 163),
    ("Previous track", 0xB6, 165),
    ("Stop", 0xB7, 166),
    ("Mute", 0xE2, 113),
    ("Volume up", 0xE9, 115),
    ("Volume down", 0xEA, 114),
    ("Brightness up", 0x6F, 225),
    ("Brightness down", 0x70, 224),
    ("Calculator", 0x192, 140),
    ("Mail", 0x18A, 155),
    ("Media player", 0x183, 171),
    ("File browser", 0x194, 144),
    ("Browser home", 0x223, 172),
    ("Browser back", 0x224, 158),
    ("Browser forward", 0x225, 159),
    ("Browser refresh", 0x227, 173),
    ("Search", 0x221, 217),
]
CONSUMER_BY_NAME = {name: (usage, ev) for name, usage, ev in _CONSUMER}
CONSUMER_NAMES = [name for name, _, _ in _CONSUMER]
CONSUMER_TO_NAME = {usage: name for name, usage, _ in _CONSUMER}
CONSUMER_TO_EVDEV = {usage: ev for _, usage, ev in _CONSUMER}

# Mouse buttons: HID button number (1-based) -> (label, evdev code)
MOUSE_BUTTONS = {
    1: ("Left click", 0x110),
    2: ("Right click", 0x111),
    3: ("Middle click", 0x112),
    4: ("Back", 0x113),
    5: ("Forward", 0x114),
    6: ("Button 6", 0x115),
    7: ("Button 7", 0x116),
    8: ("Button 8", 0x117),
}

# evdev relative axes and event types used by the uinput device
EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
EV_MSC, EV_LED, EV_REP = 0x04, 0x11, 0x14
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0x00, 0x01, 0x06, 0x08
REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0x0B, 0x0C


def modifiers_label(mask: int) -> str:
    return "+".join(MODIFIER_LABELS[i] for i in range(8) if mask & (1 << i))


def key_label(modifiers: int, hid: int) -> str:
    name = HID_TO_NAME.get(hid, f"key {hid:#04x}") if hid else ""
    mods = modifiers_label(modifiers)
    return "+".join(x for x in (mods, name) if x) or "(nothing)"


def hid_for(name: str) -> int:
    return KEY_BY_NAME[name][0]


def modifier_mask(names: list[str]) -> int:
    mask = 0
    for n in names:
        mask |= 1 << MODIFIERS.index(n)
    return mask
