"""A clickable tenkeyless keyboard for painting per-key colours."""

from __future__ import annotations

from gi.repository import Gdk, Gtk

from .. import keys
from ..features import lighting

# (key name or None for a gap, width in key units); names are keys.KEY_NAMES or lighting.SPECIAL_ZONES
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
EXTRA_ROW = [("Logo", 1.5), ("Brightness", 1.5), ("Mute", 1), ("Previous", 1), ("Next", 1)]

LABELS = {
    "Grave": "`", "Minus": "-", "Equal": "=", "LeftBracket": "[", "RightBracket": "]", "Backslash": "\\",
    "Semicolon": ";", "Apostrophe": "'", "Comma": ",", "Dot": ".", "Slash": "/", "Backspace": "⌫", "Enter": "⏎",
    "LeftShift": "Shift", "RightShift": "Shift", "LeftCtrl": "Ctrl", "RightCtrl": "Ctrl", "LeftAlt": "Alt",
    "RightAlt": "AltGr", "LeftMeta": "Super", "RightMeta": "Super", "Menu": "☰", "CapsLock": "Caps",
    "PrintScreen": "PrtSc", "ScrollLock": "ScrLk", "PageUp": "PgUp", "PageDown": "PgDn", "Delete": "Del",
    "Insert": "Ins", "Up": "↑", "Down": "↓", "Left": "←", "Right": "→", "Space": "", "Brightness": "☀",
    "Previous": "⏮", "Next": "⏭", "Mute": "🔇",
}

UNIT = 4  # grid columns per key unit, so quarter-width keys line up


def zone_of(name: str) -> int | None:
    if name in lighting.SPECIAL_ZONES:
        return lighting.SPECIAL_ZONES[name]
    if name in keys.KEY_BY_NAME:
        return lighting.zone_for_hid(keys.hid_for(name))
    return None


class KeyboardView(Gtk.Box):
    """Emits ``on_paint(zone)`` when a key is clicked."""

    def __init__(self, available_zones: list[int] | None, on_paint):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.on_paint = on_paint
        self.buttons: dict[int, Gtk.Button] = {}
        self.providers: dict[int, Gtk.CssProvider] = {}
        grid = Gtk.Grid(row_spacing=4, column_spacing=0, column_homogeneous=True)
        grid.add_css_class("lgl-keyboard")
        for r, row in enumerate(TKL_ANSI + [EXTRA_ROW]):
            col = 0
            for name, width in row:
                span = int(width * UNIT)
                if name is not None:
                    zone = zone_of(name)
                    if zone is not None and (available_zones is None or zone in available_zones):
                        lab = Gtk.Label(label=LABELS.get(name, name), ellipsize=3, width_chars=1, max_width_chars=6)
                        b = Gtk.Button(child=lab)
                        b.set_tooltip_text(name)
                        b.add_css_class("lgl-key")
                        b.set_size_request(int(width * 28) - 4, 28)
                        b.set_margin_start(2)
                        b.set_margin_end(2)
                        b.connect("clicked", lambda _b, z=zone: self.on_paint(z))
                        grid.attach(b, col, r + (1 if r == len(TKL_ANSI) else 0), span, 1)
                        self.buttons[zone] = b
                col += span
            if r == len(TKL_ANSI) - 1:
                spacer = Gtk.Box()
                spacer.set_size_request(-1, 8)
                grid.attach(spacer, 0, r + 1, 1, 1)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, vscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(grid)
        self.append(scroller)

    def set_color(self, zone: int, rgb: int | None) -> None:
        b = self.buttons.get(zone)
        if b is None:
            return
        provider = self.providers.get(zone)
        if provider is None:
            provider = Gtk.CssProvider()
            b.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
            self.providers[zone] = provider
        if rgb is None:
            css = "button { }"
        else:
            r, g, bl = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
            fg = "#000" if (0.299 * r + 0.587 * g + 0.114 * bl) > 140 else "#fff"
            css = f"button {{ background: rgb({r},{g},{bl}); color: {fg}; box-shadow: none; }}"
        provider.load_from_string(css)

    def set_colors(self, colors: dict[int, int], default: int | None = None) -> None:
        for zone in self.buttons:
            self.set_color(zone, colors.get(zone, default))


def rgba_to_int(rgba: Gdk.RGBA) -> int:
    return (round(rgba.red * 255) << 16) | (round(rgba.green * 255) << 8) | round(rgba.blue * 255)


def int_to_rgba(value: int) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.parse("#%06x" % value)
    return c
