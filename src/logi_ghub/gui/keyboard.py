"""The on-screen keyboard: shows the lighting the keyboard is showing, and lets you paint keys."""

from __future__ import annotations

import gi

gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk, Pango  # noqa: E402

from .. import layout


class KeyboardView(Gtk.Widget):
    """Calls ``on_paint(zone)`` when a key is clicked (and while dragging across keys).

    Drawn with GTK snapshots, so it needs no cairo bindings.
    """

    GAP = 0.08  # gap between keys, in key units

    def __init__(self, available_zones: list[int] | None, on_paint):
        super().__init__()
        self.on_paint = on_paint
        self.caps = layout.keycaps(available_zones)
        self.buttons = {c.zone: c for c in self.caps}  # zones shown
        self.colors: dict[int, int] = {}
        self.x0, self.y0, self.x1, self.y1 = layout.bounds(self.caps)
        self.set_hexpand(True)
        self._last_painted: int | None = None
        self._layouts: dict[str, object] = {}
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
            return 360, 620, -1, -1
        width = for_size if for_size > 0 else 620
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
            if c.x <= kx <= c.x + c.w and c.y <= ky <= c.y + 1:
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
    def set_color(self, zone: int, rgb: int | None) -> None:
        if rgb is None:
            self.colors.pop(zone, None)
        else:
            self.colors[zone] = rgb
        self.queue_draw()

    def set_colors(self, colors: dict[int, int], default: int | None = None) -> None:
        new = {z: colors.get(z, default) for z in self.buttons if colors.get(z, default) is not None}
        if new != self.colors:
            self.colors = new
            self.queue_draw()

    # drawing
    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        unit, ox, oy = self._scale(width, height)
        g = self.GAP * unit
        radius = 0.12 * unit
        font = Pango.FontDescription.from_string(f"Sans {max(6, int(unit * 0.22))}")
        border = Gdk.RGBA(red=1, green=1, blue=1, alpha=0.18)
        for c in self.caps:
            x = ox + (c.x - self.x0) * unit + g / 2
            y = oy + (c.y - self.y0) * unit + g / 2
            w = c.w * unit - g
            h = unit - g
            rgb = self.colors.get(c.zone)
            fill = Gdk.RGBA()
            if rgb is None:
                fill.red, fill.green, fill.blue, fill.alpha = 0.22, 0.22, 0.24, 1
            else:
                fill.red, fill.green, fill.blue, fill.alpha = ((rgb >> 16) & 0xFF) / 255, ((rgb >> 8) & 0xFF) / 255, (rgb & 0xFF) / 255, 1
            rect = Graphene.Rect().init(x, y, w, h)
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(rect, radius)
            snapshot.push_rounded_clip(rounded)
            snapshot.append_color(fill, rect)
            snapshot.pop()
            snapshot.append_border(rounded, [1, 1, 1, 1], [border] * 4)
            label = c.label
            if label:
                lum = 0.0 if rgb is None else (0.299 * ((rgb >> 16) & 0xFF) + 0.587 * ((rgb >> 8) & 0xFF) + 0.114 * (rgb & 0xFF))
                text = Gdk.RGBA()
                text.red = text.green = text.blue = 0.05 if lum > 140 else 0.92
                text.alpha = 1
                pl = self.create_pango_layout(label)
                pl.set_font_description(font)
                tw, th = pl.get_pixel_size()
                if tw > w - 4:
                    pl.set_text(label[:3], -1)
                    tw, th = pl.get_pixel_size()
                snapshot.save()
                snapshot.translate(Graphene.Point().init(x + (w - tw) / 2, y + (h - th) / 2))
                snapshot.append_layout(pl, text)
                snapshot.restore()


def rgba_to_int(rgba: Gdk.RGBA) -> int:
    return (round(rgba.red * 255) << 16) | (round(rgba.green * 255) << 8) | round(rgba.blue * 255)


def int_to_rgba(value: int) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.parse("#%06x" % value)
    return c
