"""Ready-made animations for the keyboard's custom-animation slot."""

from __future__ import annotations

from . import layout
from .design import Design, Layer

# A high-contrast Python syntax theme, as LED colours (saturated, so they read on black keycaps):
# letters blue like strings, digits green like numbers, special keys yellow; the rest follows
# the owner's G HUB lighting (red Esc/Enter/Backspace/Super, ...), with purple F-keys.
PY_STRING = 0x3C96FF  # letters
PY_NUMBER = 0x00FF00  # 0-9: terminal green
PY_YELLOW = 0xFFEC00  # ` - = [ ] \ ; ' , . /
PY_RED = 0xFF1010  # Esc, Backspace, Enter, Super
PY_WHITE = 0xFFFFFF  # Tab, Caps, Shift, Ctrl, Alt, Menu
PY_PURPLE = 0xB000D8  # F1-F12
PY_SKY = 0x40B0FF  # Space
PY_MAGENTA = 0xFF20C0  # media keys and brightness

# Romanian flag, tuned for LEDs (the printed blue is too dark to show)
RO_BLUE, RO_YELLOW, RO_RED = 0x0030FF, 0xFFC800, 0xFF0010
SKY = 0x50B0FF  # light blue behind the flag (paler towards the top)

# The flag covers the whole block right of the main keys, in three columns (blue, yellow, red)
NAV_COLUMNS = [["PrintScreen", "Insert", "Delete", "Left"], ["ScrollLock", "Home", "End", "Up", "Down"],
               ["Pause", "PageUp", "PageDown", "Right"]]


def _python_groups() -> list[tuple[str, list[str], int]]:
    return [
        ("Letters (strings)", list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), PY_STRING),
        ("Numbers", list("1234567890"), PY_NUMBER),
        ("Modifier keys", ["LeftCtrl", "RightCtrl", "LeftShift", "RightShift", "LeftAlt", "RightAlt", "Menu", "Tab",
                           "CapsLock"], PY_WHITE),
        ("Esc, Backspace, Enter, Super", ["Esc", "Backspace", "Enter", "LeftMeta", "RightMeta"], PY_RED),
        ("Punctuation and symbols", ["Grave", "Minus", "Equal", "Slash", "Backslash", "Comma", "Dot", "Semicolon",
                                     "Apostrophe", "LeftBracket", "RightBracket"], PY_YELLOW),
        ("F keys", [f"F{i}" for i in range(1, 13)], PY_PURPLE),
        ("Space", ["Space"], PY_SKY),
    ]


def python_romania_design() -> Design:
    """Python-theme keys; on the block right of the main keys (PrtSc to the arrows) the Romanian flag
    rises row by row out of a light blue sky, waves, and fades back; and the flag colours fall in
    bands from left to right across the logo, brightness and music keys."""
    layers = [Layer(name, keys, "static", [colour]) for name, keys, colour in _python_groups()]
    layers.append(Layer("Romanian flag", [k for col in NAV_COLUMNS for k in col], "raise", [RO_BLUE, RO_YELLOW, RO_RED],
                        period_ms=6000, direction="right", floor=0.45, background=SKY))
    layers.append(Layer("Logo and music keys", ["Logo", "Brightness", "Previous", "Play/Pause", "Next", "Mute"], "sequence",
                        [RO_BLUE, RO_YELLOW, RO_RED], period_ms=3000, direction="right", fade=True, spread=0.6))
    return Design("Python + Romania", layers)


def romania_canada_design() -> Design:
    """The whole keyboard as one big Romanian flag, rippling, then a Canadian flag, in turn; on the
    right-hand block a small flag of the same country is raised, waves and fades each time."""
    every = [c.name for c in layout.keycaps()]
    right_block = [k for col in NAV_COLUMNS for k in col]
    return Design("Romania + Canada", [
        Layer("Flags", every, "flags", [], period_ms=6000, floor=0.55, spread=1.5, flags=["romania", "canada"]),
        # on the block right of the main keys: a small flag rises out of a light blue sky, waves and
        # fades, the Romanian one while the big Romanian flag shows, then the Canadian one
        Layer("Flag raising", right_block, "raise", [], period_ms=6000, direction="right", floor=0.45, background=SKY,
              flags=["romania", "canada"]),
    ])


# Ready-made designs to start from: key -> (title, design factory)
DESIGNS = {
    "python-romania": ("Python + Romania", python_romania_design),
    "romania-canada": ("Romania + Canada", romania_canada_design),
}
