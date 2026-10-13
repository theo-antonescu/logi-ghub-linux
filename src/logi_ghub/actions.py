"""Button/key actions, shared by on-board profiles and host profiles.

On the device a binding is 4 bytes (HID++ 0x8100 profile format):

  80 01 mm mm   mouse button bitmask (big endian, bit 0 = left)
  80 02 mo kk   keyboard key: modifier bitmask, HID usage
  80 03 uu uu   consumer control usage
  90 ff 00 dd   special function ff with data dd
  0s ss aa aa   run macro stored at sector s, byte offset a
  00 00 00 00   G HUB's "disabled"
  ff ff ff ff   unassigned slot
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import keys

SPECIALS = {
    0x01: "Scroll left",
    0x02: "Scroll right",
    0x03: "DPI up",
    0x04: "DPI down",
    0x05: "DPI cycle",
    0x06: "DPI default",
    0x07: "DPI shift (sniper)",
    0x08: "Next profile",
    0x09: "Previous profile",
    0x0A: "Cycle profile",
    0x0B: "G-Shift",
    0x0C: "Battery status",
    0x0D: "Select profile",
    0x0E: "Mode switch",
    0x0F: "Host button",
    0x10: "Scroll down",
    0x11: "Scroll up",
}
SPECIAL_BY_NAME = {v: k for k, v in SPECIALS.items()}

KINDS = ("default", "none", "mouse", "key", "consumer", "special", "macro", "raw")


@dataclass
class Action:
    kind: str = "default"
    button: int = 0  # mouse: 1-based HID button
    modifiers: int = 0  # key: modifier bitmask
    key: int = 0  # key: HID usage
    usage: int = 0  # consumer usage
    function: int = 0  # special function code
    data: int = 0  # special: e.g. profile number for "Select profile"
    macro: str = ""  # name of a macro in the profile
    sector: int = 0  # on-board macro location (filled when written)
    address: int = 0
    raw: str = ""  # hex of an undecoded binding

    # -- constructors -----------------------------------------------------
    @classmethod
    def mouse(cls, button: int) -> "Action":
        return cls("mouse", button=button)

    @classmethod
    def keypress(cls, key: int | str, modifiers: int | list[str] = 0) -> "Action":
        if isinstance(key, str):
            key = keys.hid_for(key)
        if isinstance(modifiers, list):
            modifiers = keys.modifier_mask(modifiers)
        return cls("key", key=key, modifiers=modifiers)

    @classmethod
    def consumer(cls, usage: int | str) -> "Action":
        if isinstance(usage, str):
            usage = keys.CONSUMER_BY_NAME[usage][0]
        return cls("consumer", usage=usage)

    @classmethod
    def special(cls, function: int | str, data: int = 0) -> "Action":
        if isinstance(function, str):
            function = SPECIAL_BY_NAME[function]
        return cls("special", function=function, data=data)

    @classmethod
    def run_macro(cls, name: str) -> "Action":
        return cls("macro", macro=name)

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict:
        d = {"kind": self.kind}
        defaults = Action()
        for k, v in asdict(self).items():
            if k != "kind" and v != getattr(defaults, k):
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Action":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @classmethod
    def from_bytes(cls, b: bytes) -> "Action":
        b = bytes(b[:4])
        if b == b"\xff\xff\xff\xff":
            return cls("default")
        if b == b"\x00\x00\x00\x00":
            return cls("none")
        behavior = b[0] >> 4
        if b[0] == 0x80:
            if b[1] == 0x01:
                mask = (b[2] << 8) | b[3]
                if mask and mask & (mask - 1) == 0:
                    return cls.mouse(mask.bit_length())
            elif b[1] == 0x02:
                return cls("key", modifiers=b[2], key=b[3])
            elif b[1] == 0x03:
                return cls("consumer", usage=(b[2] << 8) | b[3])
            elif b[1] == 0x00:
                return cls("none")
        elif b[0] == 0x90:
            return cls("special", function=b[1], data=b[3])
        elif behavior == 0x0:
            return cls("macro", sector=((b[0] & 0x0F) << 8) | b[1], address=(b[2] << 8) | b[3])
        return cls("raw", raw=b.hex())

    def to_bytes(self) -> bytes:
        k = self.kind
        if k == "default":
            return b"\xff\xff\xff\xff"
        if k == "none":
            return b"\x00\x00\x00\x00"
        if k == "mouse":
            mask = 1 << (self.button - 1)
            return bytes([0x80, 0x01, mask >> 8, mask & 0xFF])
        if k == "key":
            return bytes([0x80, 0x02, self.modifiers & 0xFF, self.key & 0xFF])
        if k == "consumer":
            return bytes([0x80, 0x03, self.usage >> 8, self.usage & 0xFF])
        if k == "special":
            return bytes([0x90, self.function, 0x00, self.data & 0xFF])
        if k == "macro":
            if not self.sector:
                raise ValueError(f"macro {self.macro!r} has no on-board location yet")
            return bytes([(self.sector >> 8) & 0x0F, self.sector & 0xFF, self.address >> 8, self.address & 0xFF])
        if k == "raw":
            return bytes.fromhex(self.raw)
        raise ValueError(f"unknown action kind {k!r}")

    # -- display ----------------------------------------------------------
    def label(self) -> str:
        k = self.kind
        if k == "default":
            return "Default"
        if k == "none":
            return "Disabled"
        if k == "mouse":
            return keys.MOUSE_BUTTONS.get(self.button, (f"Mouse button {self.button}",))[0]
        if k == "key":
            return keys.key_label(self.modifiers, self.key)
        if k == "consumer":
            return keys.CONSUMER_TO_NAME.get(self.usage, f"Consumer {self.usage:#05x}")
        if k == "special":
            name = SPECIALS.get(self.function, f"Function {self.function:#04x}")
            return f"{name} {self.data}" if self.function == 0x0D else name
        if k == "macro":
            return f"Macro: {self.macro}" if self.macro else f"On-board macro @{self.sector:#x}:{self.address}"
        return f"Raw {self.raw}"


@dataclass
class ButtonSlot:
    """Describes one physical control so the UI can name it."""

    index: int  # 0-based binding index in the profile
    name: str
    gshift: bool = False
    tags: list[str] = field(default_factory=list)


def parse_action(text: str) -> Action:
    """Parse the CLI form of an action.

    ``default``, ``none``, ``mouse:4``, ``key:Ctrl+Shift+T``, ``consumer:Mute``,
    ``special:DPI cycle``, ``special:Select profile:2``, ``macro:NAME``.
    """
    from .macros import parse_chord

    t = text.strip()
    low = t.lower()
    if low in ("default", "none", "disabled"):
        return Action("none" if low != "default" else "default")
    kind, _, rest = t.partition(":")
    kind = kind.lower()
    if kind == "mouse":
        return Action.mouse(int(rest))
    if kind == "key":
        mods, key = parse_chord(rest)
        if key in keys.MODIFIERS:
            return Action("key", modifiers=keys.modifier_mask(mods + [key]), key=0)
        return Action.keypress(key, mods)
    if kind == "consumer":
        return Action.consumer(rest.strip())
    if kind == "special":
        name, _, data = rest.partition(":")
        match = {k.lower(): v for k, v in SPECIAL_BY_NAME.items()}
        if name.strip().lower() not in match:
            raise ValueError(f"unknown special function {name!r}; choose from: {', '.join(SPECIALS.values())}")
        return Action.special(match[name.strip().lower()], int(data) if data else 0)
    if kind == "macro":
        return Action.run_macro(rest.strip())
    if kind == "raw":
        return Action("raw", raw=rest.strip())
    raise ValueError(f"cannot parse action {text!r}")
