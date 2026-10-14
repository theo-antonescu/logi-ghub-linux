"""Ready-made animations for the keyboard's custom-animation slot."""

from __future__ import annotations

import colorsys
import math

from . import layout
from .lightfx import scale

# A high-contrast Python syntax theme, as LED colours (saturated, so they read on black keycaps):
# letters blue like strings, digits green like numbers, special keys yellow; the rest follows
# the owner's G HUB lighting (red Esc/Enter/Backspace/Super, ...), with purple F-keys.
PY_STRING = 0x1E78FF  # letters
PY_NUMBER = 0x00FF00  # 0-9: terminal green
PY_SPECIAL = 0xFFC000  # Tab, Caps, Shift, Ctrl, Alt, Menu, `
PY_RED = 0xFF1010  # Esc, Backspace, Enter, Super
PY_PUNCT = 0xFFFFFF  # punctuation and - = / \
PY_PURPLE = 0x9000FF  # F1-F12
PY_SKY = 0x40B0FF  # Space, arrows
PY_DEEP_BLUE = 0x3060FF  # PrtSc, ScrLk, Pause
PY_MAGENTA = 0xFF20C0  # media keys and brightness

# Romanian flag, tuned for LEDs (the printed blue is too dark to show)
RO_BLUE, RO_YELLOW, RO_RED = 0x0030FF, 0xFFC800, 0xFF0010

NAV_COLUMNS = [["Insert", "Delete"], ["Home", "End"], ["PageUp", "PageDown"]]


def python_theme(zones: set[int]) -> dict[int, int]:
    colours: dict[str, int] = {}
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        colours[ch] = PY_STRING
    for d in "1234567890":
        colours[d] = PY_NUMBER
    for k in ("LeftCtrl", "RightCtrl", "LeftShift", "RightShift", "LeftAlt", "RightAlt", "Menu", "Tab", "CapsLock", "Grave"):
        colours[k] = PY_SPECIAL
    for k in ("Esc", "Backspace", "Enter", "LeftMeta", "RightMeta"):
        colours[k] = PY_RED
    for k in ("Minus", "Equal", "Slash", "Backslash", "Comma", "Dot", "Semicolon", "Apostrophe", "LeftBracket", "RightBracket"):
        colours[k] = PY_PUNCT
    for i in range(1, 13):
        colours[f"F{i}"] = PY_PURPLE
    for k in ("Space", "Up", "Down", "Left", "Right"):
        colours[k] = PY_SKY
    for k in ("PrintScreen", "ScrollLock", "Pause"):
        colours[k] = PY_DEEP_BLUE
    for k in ("Brightness", "Mute", "Previous", "Play/Pause", "Next"):
        colours[k] = PY_MAGENTA
    out = {}
    for name, c in colours.items():
        z = layout.zone_of(name)
        if z is not None and z in zones:
            out[z] = c
    return out


def python_romania(zones: list[int], period_ms: int = 48, loop_ms: int = 7200, wave_ms: int = 2400):
    """Python-theme keys, a waving Romanian flag on Ins/Home/PgUp/Del/End/PgDn, and a rainbow on the
    logo and the media and brightness keys.

    The flag's stripes stay in order (blue, yellow, red from left to right); a ripple of
    brightness runs across them like wind through cloth. Returns frames for
    features.rgb_store.build_file.
    """
    zs = set(zones)
    base = {z: 0 for z in zs}  # anything without a role stays dark
    base.update(python_theme(zs))
    flag = [RO_BLUE, RO_YELLOW, RO_RED]
    stripes = []  # (zone, colour, ripple phase)
    for col_index, col in enumerate(NAV_COLUMNS):
        for row_index, name in enumerate(col):
            z = layout.zone_of(name)
            if z in zs:
                # the ripple reaches the bottom row a little later, as a flag's free edge lags
                stripes.append((z, flag[col_index], col_index * 0.22 + row_index * 0.1))
    # the logo and the media/brightness keys share one rainbow
    rainbow = [z for z in (layout.zone_of(k) for k in ("Logo", "Brightness", "Mute", "Previous", "Play/Pause", "Next")) if z in zs]
    frames = []
    for i in range(loop_ms // period_ms):
        t = i * period_ms
        colours = dict(base)
        for z, colour, phase in stripes:
            ripple = 0.5 + 0.5 * math.sin(2 * math.pi * (t / wave_ms - phase))
            colours[z] = scale(colour, 0.3 + 0.7 * ripple ** 1.5)
        r, g, b = colorsys.hsv_to_rgb((t / loop_ms) % 1.0, 1.0, 1.0)
        for z in rainbow:
            colours[z] = (round(r * 255) << 16) | (round(g * 255) << 8) | round(b * 255)
        frames.append((colours, 1))
    return frames


PRESETS = {
    # key: (title, frame generator, frame period in ms)
    "python-romania": ("Python + Romania", python_romania, 48),
}
