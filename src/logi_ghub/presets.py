"""Ready-made animations for the keyboard's custom-animation slot."""

from __future__ import annotations

import colorsys

from . import layout
from .lightfx import sequence_color

# A high-contrast Python syntax theme, as LED colours (saturated, so they read on black keycaps)
PY_IDENTIFIER = 0x40C8FF  # names: letters
PY_NUMBER = 0x30FF60  # 0-9
PY_KEYWORD = 0xD020FF  # modifiers, Tab, Caps, Esc
PY_STRING = 0xFF8C00  # ' " `
PY_OPERATOR = 0xFF2040  # - = / \
PY_PUNCT = 0xFFFFFF  # , . ; [ ]
PY_BUILTIN = 0x00FFFF  # F-keys, arrows
PY_COMMENT = 0x50A050  # PrtSc, ScrLk, Pause
PY_BLUE, PY_YELLOW = 0x2050FF, 0xFFD43B  # the Python logo

# Romanian flag, tuned for LEDs (the printed blue is too dark to show)
RO_BLUE, RO_YELLOW, RO_RED = 0x0030FF, 0xFFC800, 0xFF0010

NAV_COLUMNS = [["Insert", "Delete"], ["Home", "End"], ["PageUp", "PageDown"]]


def python_theme(zones: set[int]) -> dict[int, int]:
    colours: dict[str, int] = {}
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        colours[ch] = PY_IDENTIFIER
    for d in "1234567890":
        colours[d] = PY_NUMBER
    for k in ("LeftCtrl", "RightCtrl", "LeftShift", "RightShift", "LeftAlt", "RightAlt", "LeftMeta", "RightMeta",
              "Menu", "Tab", "CapsLock", "Esc"):
        colours[k] = PY_KEYWORD
    for k in ("Apostrophe", "Grave"):
        colours[k] = PY_STRING
    for k in ("Minus", "Equal", "Slash", "Backslash"):
        colours[k] = PY_OPERATOR
    for k in ("Comma", "Dot", "Semicolon", "LeftBracket", "RightBracket"):
        colours[k] = PY_PUNCT
    for i in range(1, 13):
        colours[f"F{i}"] = PY_BUILTIN
    for k in ("Up", "Down", "Left", "Right"):
        colours[k] = PY_BUILTIN
    for k in ("PrintScreen", "ScrollLock", "Pause"):
        colours[k] = PY_COMMENT
    colours.update({"Space": PY_BLUE, "Enter": PY_YELLOW, "Backspace": PY_YELLOW,
                    "Brightness": PY_YELLOW, "Mute": PY_BLUE, "Previous": PY_YELLOW, "Play/Pause": PY_BLUE, "Next": PY_YELLOW})
    out = {}
    for name, c in colours.items():
        z = layout.zone_of(name)
        if z is not None and z in zones:
            out[z] = c
    return out


def python_romania(zones: list[int], period_ms: int = 32, loop_ms: int = 4800, wave_step_ms: int = 400):
    """Python-theme keys, a Romanian-flag wave across Ins/Home/PgUp/Del/End/PgDn, a rainbow logo.

    Returns frames for features.rgb_store.build_file.
    """
    zs = set(zones)
    base = {z: 0 for z in zs}  # anything without a role stays dark
    base.update(python_theme(zs))
    nav = [[layout.zone_of(k) for k in col if layout.zone_of(k) in zs] for col in NAV_COLUMNS]
    logo = layout.zone_of("Logo")
    flag = [RO_BLUE, RO_YELLOW, RO_RED]
    frames = []
    for i in range(loop_ms // period_ms):
        t = i * period_ms
        colours = dict(base)
        for col_index, col in enumerate(nav):
            # the flag scrolls left to right: each column is one stripe behind the one on its left
            c = sequence_color(flag, t / wave_step_ms - col_index, fade=True)
            for z in col:
                colours[z] = c
        if logo in zs:
            r, g, b = colorsys.hsv_to_rgb((t / loop_ms) % 1.0, 1.0, 1.0)
            colours[logo] = (round(r * 255) << 16) | (round(g * 255) << 8) | round(b * 255)
        frames.append((colours, 1))
    return frames


PRESETS = {
    "python-romania": ("Python + Romania", python_romania),
}
