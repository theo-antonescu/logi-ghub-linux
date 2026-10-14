"""Where the primary monitor sits on the desktop, for scripts' MoveMouseTo.

On Windows, MoveMouseTo's 0-65535 covers the primary monitor and
MoveMouseToVirtual covers every monitor. The virtual pointer always spans
the whole desktop, so MoveMouseTo has to be scaled into the primary
monitor's part of it. GNOME tells us the layout over D-Bus; on other
desktops (or if the call fails) both functions cover the whole desktop.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

Rect = tuple[float, float, float, float]  # x, y, width, height


def _layout() -> tuple[list[tuple[Rect, bool]], int]:
    """(logical monitor rectangle, is primary) for each monitor, from Mutter."""
    from gi.repository import Gio

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    reply = bus.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                          "org.gnome.Mutter.DisplayConfig", "GetCurrentState", None, None,
                          Gio.DBusCallFlags.NONE, 1000, None)
    _serial, monitors, logical, props = reply.unpack()
    logical_layout = props.get("layout-mode", 1) == 1  # 1: sizes already divided by the scale
    sizes = {}
    for (connector, *_), modes, _props in monitors:
        for _id, w, h, _rate, _pref, _scales, mprops in modes:
            if mprops.get("is-current"):
                sizes[connector] = (w, h)
    out = []
    for x, y, scale, transform, primary, mons, _props in logical:
        w, h = sizes.get(mons[0][0], (0, 0))
        if transform % 2:  # rotated 90 or 270 degrees
            w, h = h, w
        if logical_layout:
            w, h = w / scale, h / scale
        out.append(((x, y, w, h), primary))
    return out, len(out)


def primary_fraction() -> Rect | None:
    """The primary monitor as fractions (x, y, width, height) of the desktop, or None if unknown."""
    try:
        monitors, n = _layout()
    except Exception as e:  # not GNOME, no session bus, no gi
        log.debug("monitor layout unavailable: %s", e)
        return None
    if n < 2:
        return None
    x0 = min(r[0] for r, _ in monitors)
    y0 = min(r[1] for r, _ in monitors)
    x1 = max(r[0] + r[2] for r, _ in monitors)
    y1 = max(r[1] + r[3] for r, _ in monitors)
    primary = next((r for r, p in monitors if p), None)
    if primary is None or x1 <= x0 or y1 <= y0:
        return None
    px, py, pw, ph = primary
    return (px - x0) / (x1 - x0), (py - y0) / (y1 - y0), pw / (x1 - x0), ph / (y1 - y0)


def to_desktop(x: int, y: int, area: Rect | None) -> tuple[int, int]:
    """Scale a 0-65535 position on the primary monitor to 0-65535 across the desktop."""
    if area is None:
        return x, y
    fx, fy, fw, fh = area
    return round((fx + x / 65535 * fw) * 65535), round((fy + y / 65535 * fh) * 65535)
