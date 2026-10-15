"""Ready-made animations for the keyboard's custom-animation slot."""

from __future__ import annotations

from .design import Design, Layer

# A high-contrast Python syntax theme, as LED colours (saturated, so they read on black keycaps):
# letters blue like strings, digits green like numbers, special keys yellow; the rest follows
# the owner's G HUB lighting (red Esc/Enter/Backspace/Super, ...), with purple F-keys.
PY_STRING = 0x3C96FF  # letters
PY_NUMBER = 0x00FF00  # 0-9: terminal green
PY_YELLOW = 0xFFEC00  # ` - = [ ] \ ; ' , . /
PY_RED = 0xFF1010  # Esc, Backspace, Enter, Super
PY_WHITE = 0xFFFFFF  # Tab, Caps, Shift, Ctrl, Alt, Menu, PrtSc, ScrLk, Pause
PY_PURPLE = 0xB000D8  # F1-F12
PY_SKY = 0x40B0FF  # Space, arrows
PY_MAGENTA = 0xFF20C0  # media keys and brightness

# Romanian flag, tuned for LEDs (the printed blue is too dark to show)
RO_BLUE, RO_YELLOW, RO_RED = 0x0030FF, 0xFFC800, 0xFF0010

NAV_COLUMNS = [["Insert", "Delete"], ["Home", "End"], ["PageUp", "PageDown"]]


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
        ("Space and arrows", ["Space", "Up", "Down", "Left", "Right"], PY_SKY),
        ("PrtSc, ScrLk, Pause", ["PrintScreen", "ScrollLock", "Pause"], PY_WHITE),
    ]


def python_romania_design() -> Design:
    """Python-theme keys, a waving Romanian flag on Ins/Home/PgUp/Del/End/PgDn, and a rainbow on the
    logo and the media and brightness keys. The flag's stripes stay in order (blue, yellow, red from
    left to right) while a ripple of brightness runs across them like wind through cloth."""
    layers = [Layer(name, keys, "static", [colour]) for name, keys, colour in _python_groups()]
    layers.append(Layer("Romanian flag", [k for col in NAV_COLUMNS for k in col], "ripple", [RO_BLUE, RO_YELLOW, RO_RED],
                        period_ms=2400, direction="right", floor=0.6, spread=0.44))
    layers.append(Layer("Rainbow", ["Logo", "Brightness", "Mute", "Previous", "Play/Pause", "Next"], "rainbow",
                        [0xFF0000], period_ms=7200, direction="none", spread=0.0))
    return Design("Python + Romania", layers)


def python_romania(zones: list[int], period_ms: int = 48):
    """Frames of the Python + Romania design (kept for callers that store frames directly)."""
    return python_romania_design().renderer(zones).frames(period_ms)


# Ready-made designs to start from: key -> (title, design factory)
DESIGNS = {
    "python-romania": ("Python + Romania", python_romania_design),
}
PRESETS = {
    # key: (title, frame generator, frame period in ms)
    "python-romania": ("Python + Romania", python_romania, 48),
}
