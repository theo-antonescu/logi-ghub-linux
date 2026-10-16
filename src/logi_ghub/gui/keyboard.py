"""The on-screen keyboard: shows the lighting the keyboard is showing, and lets you paint keys."""

from __future__ import annotations

import gi

gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk, Pango  # noqa: E402

from .. import layout


def _rgba(r: float, g: float, b: float, a: float = 1.0) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def _from_int(rgb: int, a: float = 1.0) -> Gdk.RGBA:
    return _rgba(((rgb >> 16) & 0xFF) / 255, ((rgb >> 8) & 0xFF) / 255, (rgb & 0xFF) / 255, a)


BODY = _rgba(0.20, 0.22, 0.25)  # the keyboard's aluminium plate
BODY_EDGE = _rgba(0.36, 0.38, 0.42)
CAP = _rgba(0.075, 0.08, 0.09)  # black keycaps
CAP_TOP = _rgba(0.11, 0.115, 0.13)
CAP_EDGE = _rgba(1, 1, 1, 0.10)
UNLIT = _rgba(0.36, 0.38, 0.41)  # legend of a key that is off
SELECTED = _rgba(1, 1, 1, 0.9)


class KeyboardView(Gtk.Widget):
    """Calls ``on_paint(zone)`` when a key is clicked (and while dragging across keys).

    Drawn like the keyboard looks: black keycaps on a dark plate, with the light
    showing through each key's legend. Uses GTK snapshots, so it needs no cairo bindings.
    """

    GAP = 0.08  # gap between keys, in key units
    PAD = 0.35  # plate border around the keys, in key units

    def __init__(self, available_zones: list[int] | None, on_paint):
        super().__init__()
        self.on_paint = on_paint
        self.caps = layout.keycaps(available_zones)
        self.buttons = {c.zone: c for c in self.caps}  # zones shown
        self.colors: dict[int, int] = {}
        self.selected: set[int] = set()  # zones drawn with an outline (the keys of the layer being edited)
        x0, y0, x1, y1 = layout.bounds(self.caps, decor=True)
        self.x0, self.y0, self.x1, self.y1 = x0 - self.PAD, y0 - self.PAD, x1 + self.PAD, y1 + self.PAD
        self.set_hexpand(True)
        self._last_painted: int | None = None
        click = Gtk.GestureClick()
        click.connect("pressed", lambda _g, _n, x, y: self._hit(x, y, new=True))
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda _g, x, y: setattr(self, "_drag_origin", (x, y)))
        drag.connect("drag-update", self._drag)
        self.add_controller(drag)
        self._drag_origin = (0.0, 0.0)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

    def do_measure(self, orientation, for_size):
        aspect = (self.y1 - self.y0) / (self.x1 - self.x0)
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 360, 720, -1, -1
        width = for_size if for_size > 0 else 720
        h = int(width * aspect) + 8
        return h, h, -1, -1

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    # geometry
    def _scale(self, width: int, height: int) -> tuple[float, float, float]:
        unit = min((width - 8) / (self.x1 - self.x0), (height - 8) / (self.y1 - self.y0))
        ox = (width - unit * (self.x1 - self.x0)) / 2
        oy = (height - unit * (self.y1 - self.y0)) / 2
        return unit, ox, oy

    def _zone_at(self, x: float, y: float) -> int | None:
        unit, ox, oy = self._scale(self.get_width(), self.get_height())
        kx = (x - ox) / unit + self.x0
        ky = (y - oy) / unit + self.y0
        for c in self.caps:
            if c.x <= kx <= c.x + c.w and c.y <= ky <= c.y + c.h:
                return c.zone
        return None

    def _hit(self, x: float, y: float, new: bool = False):
        zone = self._zone_at(x, y)
        if zone is not None and self.on_paint and (new or zone != self._last_painted):
            self._last_painted = zone
            self.on_paint(zone)

    def _drag(self, _g, dx: float, dy: float):
        ox, oy = self._drag_origin
        self._hit(ox + dx, oy + dy)

    # colours
    def set_selected(self, zones) -> None:
        zones = set(zones)
        if zones != self.selected:
            self.selected = zones
            self.queue_draw()

    def set_colors(self, colors: dict[int, int], default: int | None = None) -> None:
        new = {z: colors.get(z, default) for z in self.buttons if colors.get(z, default) is not None}
        if new != self.colors:
            self.colors = new
            self.queue_draw()

    # drawing
    @staticmethod
    def _rounded(x, y, w, h, r) -> Gsk.RoundedRect:
        rr = Gsk.RoundedRect()
        rr.init_from_rect(Graphene.Rect().init(x, y, w, h), r)
        return rr

    def _box(self, snapshot, x, y, w, h, r, fill, edge=None):
        rr = self._rounded(x, y, w, h, r)
        snapshot.push_rounded_clip(rr)
        snapshot.append_color(fill, rr.bounds)
        snapshot.pop()
        if edge is not None:
            snapshot.append_border(rr, [1, 1, 1, 1], [edge] * 4)

    def _legend(self, snapshot, text, font, x, y, w, h, color: Gdk.RGBA, glow: bool):
        pl = self.create_pango_layout(text)
        pl.set_font_description(font)
        tw, th = pl.get_pixel_size()
        if tw > w - 6:  # a long legend: smaller type, as on the keycaps
            smaller = font.copy()
            smaller.set_size(int(font.get_size() * max(0.6, (w - 6) / tw)))
            pl.set_font_description(smaller)
            tw, th = pl.get_pixel_size()
        point = Graphene.Point().init(x + (w - tw) / 2, y + (h - th) / 2)
        if glow:  # light bleeding around the lit legend
            snapshot.push_blur(max(2.0, h * 0.12))
            snapshot.save()
            snapshot.translate(point)
            snapshot.append_layout(pl, color)
            snapshot.restore()
            snapshot.pop()
        snapshot.save()
        snapshot.translate(point)
        snapshot.append_layout(pl, color)
        snapshot.restore()

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        unit, ox, oy = self._scale(width, height)
        px = lambda kx: ox + (kx - self.x0) * unit  # noqa: E731
        py = lambda ky: oy + (ky - self.y0) * unit  # noqa: E731
        g = self.GAP * unit
        # the plate
        self._box(snapshot, px(self.x0), py(self.y0), (self.x1 - self.x0) * unit, (self.y1 - self.y0) * unit,
                  0.3 * unit, BODY, BODY_EDGE)
        small = Pango.FontDescription.from_string(f"Sans Bold {max(5, int(unit * 0.14))}")
        for kind, kx, ky, kw, kh in layout.DECOR:
            x, y, w, h = px(kx), py(ky), kw * unit, kh * unit
            if kind == "button":
                self._box(snapshot, x, y, w, h, w / 2, CAP, CAP_EDGE)
            elif kind == "badge":
                self._box(snapshot, x, y, w, h, 0.12 * unit, _rgba(0.13, 0.14, 0.16), CAP_EDGE)
                self._legend(snapshot, "LIGHTSPEED", small, x, y, w, h, UNLIT, False)
            elif kind == "roller":
                self._box(snapshot, x, y, w, h, 0.15 * unit, _rgba(0.05, 0.05, 0.06), CAP_EDGE)
                for i in range(1, 9):  # knurled volume wheel
                    snapshot.append_color(_rgba(1, 1, 1, 0.07), Graphene.Rect().init(x + w * 0.12, y + h * i / 9, w * 0.76, 1))
        font = Pango.FontDescription.from_string(f"Sans {max(6, int(unit * 0.24))}")
        logo_font = Pango.FontDescription.from_string(f"Sans Bold {max(8, int(unit * 0.62))}")
        for c in self.caps:
            rgb = self.colors.get(c.zone)
            lit = rgb is not None and rgb != 0
            legend = _from_int(rgb) if lit else UNLIT
            x, y = px(c.x) + g / 2, py(c.y) + g / 2
            w, h = c.w * unit - g, c.h * unit - g
            if c.name == "Logo":
                self._legend(snapshot, "G", logo_font, x, y, w, h, legend, lit)
                continue
            if c.round:
                self._box(snapshot, x, y, w, h, w / 2, CAP, CAP_EDGE)
            else:
                self._box(snapshot, x, y, w, h, 0.12 * unit, CAP, CAP_EDGE)
                inset = 0.07 * unit  # the keycap's top face
                self._box(snapshot, x + inset, y + inset * 0.6, w - 2 * inset, h - 2.2 * inset, 0.1 * unit, CAP_TOP)
            if lit:  # a faint underglow from the LED below the key
                glow = self._rounded(x + w * 0.2, y + h * 0.82, w * 0.6, h * 0.1, h * 0.05)
                snapshot.push_rounded_clip(glow)
                snapshot.append_color(_from_int(rgb, 0.35), glow.bounds)
                snapshot.pop()
            if c.label:
                self._legend(snapshot, c.label, font, x, y - (0 if c.round else 0.03 * unit), w, h, legend, lit)
            if c.zone in self.selected:
                rr = self._rounded(x - 1, y - 1, w + 2, h + 2, (w / 2 if c.round else 0.12 * unit) + 1)
                snapshot.append_border(rr, [2.5] * 4, [SELECTED] * 4)


def rgba_to_int(rgba: Gdk.RGBA) -> int:
    return (round(rgba.red * 255) << 16) | (round(rgba.green * 255) << 8) | round(rgba.blue * 255)


def int_to_rgba(value: int) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.parse("#%06x" % value)
    return c
