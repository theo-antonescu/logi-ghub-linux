"""Macros: a named list of steps, playable by the host daemon or stored on the device.

Text form (used by the CLI and shown in the GUI), steps separated by commas::

    Ctrl+C, 40ms, Ctrl+V          key chords, tapped
    down:LeftShift, A, up:LeftShift
    "hello world"                 type text (US layout)
    mouse:1                        click a mouse button
    consumer:Volume up             media key
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from . import keys
from .features import onboard as ob

STEP_TYPES = ("down", "up", "tap", "delay", "text", "mouse", "consumer")
REPEAT_MODES = ("once", "while_held", "toggle")

_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "Minus", "+": "Equal", "{": "LeftBracket", "}": "RightBracket", "|": "Backslash", ":": "Semicolon",
    '"': "Apostrophe", "~": "Grave", "<": "Comma", ">": "Dot", "?": "Slash",
}
_PLAIN = {
    " ": "Space", "\n": "Enter", "\t": "Tab", "-": "Minus", "=": "Equal", "[": "LeftBracket", "]": "RightBracket",
    "\\": "Backslash", ";": "Semicolon", "'": "Apostrophe", "`": "Grave", ",": "Comma", ".": "Dot", "/": "Slash",
}


def char_to_key(ch: str) -> tuple[str, bool]:
    """Return (key name, needs shift) for a character on a US layout."""
    if ch.isalpha() and ch.isascii():
        return ch.upper(), ch.isupper()
    if ch.isdigit():
        return ch, False
    if ch in _PLAIN:
        return _PLAIN[ch], False
    if ch in _SHIFTED:
        return _SHIFTED[ch], True
    raise ValueError(f"cannot type {ch!r} with a US keyboard layout")


@dataclass
class Step:
    type: str
    key: str = ""  # key name, or chord "Ctrl+Shift+A" for tap
    ms: int = 0
    text: str = ""
    button: int = 0
    usage: str = ""

    def to_dict(self) -> dict:
        d = {"type": self.type}
        for k in ("key", "ms", "text", "button", "usage"):
            v = getattr(self, k)
            if v:
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def __str__(self) -> str:
        if self.type == "tap":
            return self.key
        if self.type in ("down", "up"):
            return f"{self.type}:{self.key}"
        if self.type == "delay":
            return f"{self.ms}ms"
        if self.type == "text":
            return '"' + self.text.replace('"', '\\"') + '"'
        if self.type == "mouse":
            return f"mouse:{self.button}"
        if self.type == "consumer":
            return f"consumer:{self.usage}"
        return self.type


_MOD_ALIASES = {"ctrl": "LeftCtrl", "control": "LeftCtrl", "shift": "LeftShift", "alt": "LeftAlt", "super": "LeftMeta",
                "meta": "LeftMeta", "win": "LeftMeta", "altgr": "RightAlt", "rctrl": "RightCtrl", "rshift": "RightShift"}
_KEY_LOOKUP = {k.lower(): k for k in keys.KEY_NAMES}


def resolve_key(name: str) -> str:
    n = name.strip()
    low = n.lower()
    if low in _MOD_ALIASES:
        return _MOD_ALIASES[low]
    if low in _KEY_LOOKUP:
        return _KEY_LOOKUP[low]
    raise ValueError(f"unknown key {name!r}")


def parse_chord(chord: str) -> tuple[list[str], str]:
    """'Ctrl+Shift+A' -> (['LeftCtrl', 'LeftShift'], 'A'). A bare modifier is its own key."""
    parts = [p for p in re.split(r"\+(?!$)", chord.strip()) if p]
    names = [resolve_key(p) for p in parts]
    if not names:
        raise ValueError("empty key chord")
    *mods, key = names
    for m in mods:
        if m not in keys.MODIFIERS:
            raise ValueError(f"{m} is not a modifier in {chord!r}")
    return mods, key


@dataclass
class Macro:
    name: str
    steps: list[Step] = field(default_factory=list)
    repeat: str = "once"
    hold: list[str] = field(default_factory=list)  # keys held down for as long as the macro runs, e.g. ["LeftAlt"]
    interval_ms: int = 0  # pause between repeats

    def to_dict(self) -> dict:
        d = {"name": self.name, "repeat": self.repeat, "steps": [s.to_dict() for s in self.steps]}
        if self.hold:
            d["hold"] = list(self.hold)
        if self.interval_ms:
            d["interval_ms"] = self.interval_ms
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Macro":
        return cls(d["name"], [Step.from_dict(s) for s in d.get("steps", [])], d.get("repeat", "once"),
                   list(d.get("hold", [])), int(d.get("interval_ms", 0)))

    @property
    def needs_host(self) -> bool:
        """True when the device can't play it from on-board memory."""
        return self.repeat != "once" or bool(self.hold)

    def to_text(self) -> str:
        return ", ".join(str(s) for s in self.steps)

    @classmethod
    def parse(cls, name: str, text: str, repeat: str = "once", hold: str | list[str] = "", interval_ms: int = 0) -> "Macro":
        steps: list[Step] = []
        for token in _split(text):
            t = token.strip()
            if not t:
                continue
            if t.startswith('"') and t.endswith('"') and len(t) >= 2:
                steps.append(Step("text", text=t[1:-1].replace('\\"', '"')))
            elif re.fullmatch(r"\d+\s*ms", t):
                steps.append(Step("delay", ms=int(t[:-2])))
            elif t.lower().startswith(("down:", "up:")):
                kind, k = t.split(":", 1)
                steps.append(Step(kind.lower(), key=resolve_key(k)))
            elif t.lower().startswith("mouse:"):
                steps.append(Step("mouse", button=int(t.split(":", 1)[1])))
            elif t.lower().startswith("consumer:"):
                usage = t.split(":", 1)[1].strip()
                if usage not in keys.CONSUMER_BY_NAME:
                    raise ValueError(f"unknown media key {usage!r}")
                steps.append(Step("consumer", usage=usage))
            else:
                mods, key = parse_chord(t)
                steps.append(Step("tap", key="+".join(mods + [key])))
        if repeat not in REPEAT_MODES:
            raise ValueError(f"repeat must be one of {REPEAT_MODES}")
        if isinstance(hold, str):
            hold = [resolve_key(k) for k in hold.split("+") if k.strip()]
        return cls(name, steps, repeat, hold, int(interval_ms))

    # -- expansion into primitive events ------------------------------------
    def events(self) -> list[tuple[str, object]]:
        """Expand to ('key_down'|'key_up', name), ('delay', ms), ('mouse_down'|'mouse_up', n), ('consumer', name)."""
        out: list[tuple[str, object]] = []
        for s in self.steps:
            if s.type == "down":
                out.append(("key_down", s.key))
            elif s.type == "up":
                out.append(("key_up", s.key))
            elif s.type == "tap":
                mods, key = parse_chord(s.key)
                out += [("key_down", m) for m in mods] + [("key_down", key), ("key_up", key)]
                out += [("key_up", m) for m in reversed(mods)]
            elif s.type == "delay":
                out.append(("delay", s.ms))
            elif s.type == "text":
                for ch in s.text:
                    key, shift = char_to_key(ch)
                    if shift:
                        out.append(("key_down", "LeftShift"))
                    out += [("key_down", key), ("key_up", key)]
                    if shift:
                        out.append(("key_up", "LeftShift"))
            elif s.type == "mouse":
                out += [("mouse_down", s.button), ("mouse_up", s.button)]
            elif s.type == "consumer":
                out.append(("consumer", s.usage))
        return out

    # -- on-board form ----------------------------------------------------------
    def to_onboard_ops(self) -> list[tuple[int, bytes]]:
        """Encode for on-board memory. Key ops carry (modifier bits, HID usage)."""
        ops: list[tuple[int, bytes]] = []
        for ev, arg in self.events():
            if ev in ("key_down", "key_up"):
                op = ob.OP_KEY_DOWN if ev == "key_down" else ob.OP_KEY_UP
                name = str(arg)
                if name in keys.MODIFIERS:
                    ops.append((op, bytes([1 << keys.MODIFIERS.index(name), 0])))
                else:
                    ops.append((op, bytes([0, keys.hid_for(name)])))
            elif ev == "delay":
                ops.append((ob.OP_DELAY, struct.pack(">H", min(int(arg), 0xFFFF))))
            elif ev in ("mouse_down", "mouse_up"):
                mask = 1 << (int(arg) - 1)
                op = ob.OP_BUTTON_DOWN if ev == "mouse_down" else ob.OP_BUTTON_UP
                ops.append((op, struct.pack(">H", mask)))
            elif ev == "consumer":
                usage = keys.CONSUMER_BY_NAME[str(arg)][0]
                ops.append((ob.OP_CONSUMER_DOWN, struct.pack(">H", usage)))
                ops.append((ob.OP_CONSUMER_UP, struct.pack(">H", usage)))
        ops.append((ob.OP_END, b""))
        return ops

    @classmethod
    def from_onboard_ops(cls, name: str, ops: list[tuple[int, bytes]]) -> "Macro":
        steps: list[Step] = []
        for op, args in ops:
            if op in (ob.OP_KEY_DOWN, ob.OP_KEY_UP):
                kind = "down" if op == ob.OP_KEY_DOWN else "up"
                mods, usage = args[0], args[1]
                for i in range(8):
                    if mods & (1 << i):
                        steps.append(Step(kind, key=keys.MODIFIERS[i]))
                if usage:
                    steps.append(Step(kind, key=keys.HID_TO_NAME.get(usage, f"{usage:#04x}")))
            elif op == ob.OP_DELAY:
                steps.append(Step("delay", ms=struct.unpack(">H", args)[0]))
            elif op in (ob.OP_BUTTON_DOWN, ob.OP_BUTTON_UP):
                mask = struct.unpack(">H", args)[0]
                if op == ob.OP_BUTTON_DOWN and mask:
                    steps.append(Step("mouse", button=mask.bit_length()))
            elif op == ob.OP_CONSUMER_DOWN:
                usage = struct.unpack(">H", args)[0]
                steps.append(Step("consumer", usage=keys.CONSUMER_TO_NAME.get(usage, f"{usage:#x}")))
        return cls(name, _merge_taps(steps))


def _merge_taps(steps: list[Step]) -> list[Step]:
    """Fold 'down:X, up:X' pairs back into taps for readability."""
    out: list[Step] = []
    i = 0
    while i < len(steps):
        s = steps[i]
        if s.type == "down" and i + 1 < len(steps) and steps[i + 1].type == "up" and steps[i + 1].key == s.key:
            out.append(Step("tap", key=s.key))
            i += 2
            continue
        out.append(s)
        i += 1
    return out


def _split(text: str) -> list[str]:
    """Split on commas outside double quotes."""
    out, cur, quoted, esc = [], "", False, False
    for ch in text:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\" and quoted:
            cur += ch
            esc = True
        elif ch == '"':
            quoted = not quoted
            cur += ch
        elif ch == "," and not quoted:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out
