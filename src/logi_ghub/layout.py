"""Physical keyboard layout: where each lighting zone sits, for drawing and for wave animations."""

from __future__ import annotations

from dataclasses import dataclass

from . import keys
from .features import lighting

# Rows of (key name or None for a gap, width in key units). Names are keys.KEY_NAMES or lighting.SPECIAL_ZONES.
TKL_ANSI: list[list[tuple[str | None, float]]] = [
    [("Esc", 1), (None, 1), ("F1", 1), ("F2", 1), ("F3", 1), ("F4", 1), (None, 0.5), ("F5", 1), ("F6", 1), ("F7", 1),
     ("F8", 1), (None, 0.5), ("F9", 1), ("F10", 1), ("F11", 1), ("F12", 1), (None, 0.25), ("PrintScreen", 1),
     ("ScrollLock", 1), ("Pause", 1)],
    [("Grave", 1), ("1", 1), ("2", 1), ("3", 1), ("4", 1), ("5", 1), ("6", 1), ("7", 1), ("8", 1), ("9", 1), ("0", 1),
     ("Minus", 1), ("Equal", 1), ("Backspace", 2), (None, 0.25), ("Insert", 1), ("Home", 1), ("PageUp", 1)],
    [("Tab", 1.5), ("Q", 1), ("W", 1), ("E", 1), ("R", 1), ("T", 1), ("Y", 1), ("U", 1), ("I", 1), ("O", 1), ("P", 1),
     ("LeftBracket", 1), ("RightBracket", 1), ("Backslash", 1.5), (None, 0.25), ("Delete", 1), ("End", 1), ("PageDown", 1)],
    [("CapsLock", 1.75), ("A", 1), ("S", 1), ("D", 1), ("F", 1), ("G", 1), ("H", 1), ("J", 1), ("K", 1), ("L", 1),
     ("Semicolon", 1), ("Apostrophe", 1), ("Enter", 2.25)],
    [("LeftShift", 2.25), ("Z", 1), ("X", 1), ("C", 1), ("V", 1), ("B", 1), ("N", 1), ("M", 1), ("Comma", 1), ("Dot", 1),
     ("Slash", 1), ("RightShift", 2.75), (None, 1.25), ("Up", 1)],
    [("LeftCtrl", 1.25), ("LeftMeta", 1.25), ("LeftAlt", 1.25), ("Space", 6.25), ("RightAlt", 1.25), ("RightMeta", 1.25),
     ("Menu", 1.25), ("RightCtrl", 1.25), (None, 0.25), ("Left", 1), ("Down", 1), ("Right", 1)],
]

# The strip above the keys on the G915 X TKL: (name, x, width). Lit ones have a zone; the rest are drawn only.
TOP_Y = -1.45
TOP_ROW: list[tuple[str, float, float]] = [
    ("Logo", 0.1, 0.8),
    ("Brightness", 5.1, 0.8),
    ("Previous", 12.6, 0.8), ("Play/Pause", 13.6, 0.8), ("Next", 14.6, 0.8), ("Mute", 15.6, 0.8),
]
ROUND = {"Brightness", "Previous", "Play/Pause", "Next", "Mute"}
# Unlit parts of that strip, for the picture: (kind, x, y, width, height)
DECOR: list[tuple[str, float, float, float, float]] = [
    ("button", 2.1, TOP_Y, 0.8, 0.8), ("button", 3.1, TOP_Y, 0.8, 0.8), ("button", 4.1, TOP_Y, 0.8, 0.8),
    ("badge", 7.65, TOP_Y - 0.05, 2.2, 0.9),
    ("roller", 16.75, TOP_Y - 0.2, 1.45, 1.05),
]

LABELS = {
    "Grave": "`", "Minus": "-", "Equal": "=", "LeftBracket": "[", "RightBracket": "]", "Backslash": "\\",
    "Semicolon": ";", "Apostrophe": "'", "Comma": ",", "Dot": ".", "Slash": "/", "Backspace": "⌫", "Enter": "⏎",
    "LeftShift": "Shift", "RightShift": "Shift", "LeftCtrl": "Ctrl", "RightCtrl": "Ctrl", "LeftAlt": "Alt",
    "RightAlt": "AltGr", "LeftMeta": "Super", "RightMeta": "Super", "Menu": "☰", "CapsLock": "Caps",
    "PrintScreen": "PrtSc", "ScrollLock": "ScrLk", "PageUp": "PgUp", "PageDown": "PgDn", "Delete": "Del",
    "Insert": "Ins", "Up": "↑", "Down": "↓", "Left": "←", "Right": "→", "Space": "", "Brightness": "☀",
    "Previous": "⏮", "Next": "⏭", "Mute": "×", "Play/Pause": "⏯", "Pause": "Pause", "Logo": "G",
}

@dataclass(frozen=True)
class KeyCap:
    name: str
    zone: int
    x: float  # left edge, key units
    y: float  # top edge, rows
    w: float
    h: float = 1.0

    @property
    def round(self) -> bool:
        return self.name in ROUND

    @property
    def label(self) -> str:
        return LABELS.get(self.name, self.name)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2


def zone_of(name: str) -> int | None:
    if name in lighting.SPECIAL_ZONES:
        return lighting.SPECIAL_ZONES[name]
    if name in keys.KEY_BY_NAME:
        return lighting.zone_for_hid(keys.hid_for(name))
    return None


def keycaps(available_zones: list[int] | None = None) -> list[KeyCap]:
    """Key positions, with the logo and media buttons in the strip above the function row like on the keyboard."""
    caps = []

    def add(name, x, y, w, h=1.0):
        zone = zone_of(name)
        if zone is not None and (available_zones is None or zone in available_zones):
            caps.append(KeyCap(name, zone, x, y, w, h))

    for name, x, w in TOP_ROW:
        add(name, x, TOP_Y, w, w)
    for r, row in enumerate(TKL_ANSI):
        y = r + (0.25 if r else 0)  # small gap under the F-row
        x = 0.0
        for name, width in row:
            if name is not None:
                add(name, x, y, width)
            x += width
    return caps


def bounds(caps: list[KeyCap], decor: bool = False) -> tuple[float, float, float, float]:
    boxes = [(c.x, c.y, c.x + c.w, c.y + c.h) for c in caps]
    if decor:
        boxes += [(x, y, x + w, y + h) for _k, x, y, w, h in DECOR]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
