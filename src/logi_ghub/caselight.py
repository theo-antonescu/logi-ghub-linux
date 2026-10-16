"""Playing a lighting design on the rest of the computer (through OpenRGB), in step with the keyboard.

Each device's LEDs, in order, are laid along the keyboard's middle row from left to right, and each
LED shows the colour of the key under it. So a design's stripes, flutter and colour changes run
along the RAM sticks, the GPU bar, the motherboard and the cooler ring as they do across the keys.
"""

from __future__ import annotations

import logging
import threading
import time

from . import layout
from .design import Design
from .openrgb import Controller, OpenRGB

log = logging.getLogger(__name__)

SKIP_KINDS = {"Keyboard", "Mouse"}  # the app drives Logitech devices itself
# LEDs on RAM and graphics cards turn small amounts of a colour into a strong tint (a red with a touch of
# blue looks pink); a gamma curve makes their colours match the keyboard's
GAMMA = {"DRAM": 2.2, "GPU": 2.2}
# white balance per device (red, green, blue multipliers): older LEDs drift towards yellow
BALANCE = {"Kraken": (0.6, 0.7, 1.0)}


def balance_for(name: str) -> tuple[float, float, float]:
    return next((b for key, b in BALANCE.items() if key.lower() in name.lower()), (1.0, 1.0, 1.0))


def corrected(colour: int, gamma: float, balance=(1.0, 1.0, 1.0)) -> int:
    out = 0
    for shift, k in zip((16, 8, 0), balance):
        v = ((colour >> shift) & 0xFF) / 255
        out |= round(255 * min(1.0, (v ** gamma) * k)) << shift
    return out


def key_strip(n: int) -> list[int]:
    """Zones of ``n`` keys spread evenly from the left of the keyboard to the right, along the
    home row and on through the right-hand block."""
    caps = layout.keycaps()
    row = [c for c in caps if abs(c.y - 3.25) < 0.1] + [c for c in caps if c.name in ("Delete", "End", "PageDown")]
    row.sort(key=lambda c: c.cx)
    x0, x1 = row[0].cx, row[-1].cx
    out = []
    for i in range(n):
        x = x0 + (x1 - x0) * (i / max(1, n - 1))
        out.append(min(row, key=lambda c: abs(c.cx - x)).zone)
    return out


class CaseLights(threading.Thread):
    """Streams a design to every OpenRGB device until stopped. ``t0`` is the shared start time
    (time.monotonic()), so the keyboard's own player can stay in step."""

    def __init__(self, design: Design, t0: float | None = None, fps: float = 25.0, host: str = "127.0.0.1"):
        super().__init__(name="case-lights", daemon=True)
        self.design = design
        self.t0 = t0 if t0 is not None else time.monotonic()
        self.fps = fps
        self.host = host
        self.stop_event = threading.Event()
        self.dark = threading.Event()  # set while everything should be off (the user is away)
        self.devices: list[tuple[Controller, list[int]]] = []

    def run(self) -> None:
        renderer = self.design.renderer()
        try:
            with OpenRGB(self.host) as o:
                last: dict[int, list[int]] = {}
                count, checked = -1, 0.0
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    if started - checked > 5:  # OpenRGB finds devices one by one after it starts
                        checked = started
                        now = o.controller_count()
                        if now != count:
                            count = now
                            self._take_over(o)
                            last.clear()
                    frame = {} if self.dark.is_set() else renderer.frame(started - self.t0)
                    for c, zones in self.devices:
                        g = GAMMA.get(c.kind, 1.0)
                        colours = [corrected(frame.get(z, 0), g, balance_for(c.name)) for z in zones]
                        if last.get(c.index) != colours:
                            o.update_leds(c.index, colours)
                            last[c.index] = colours
                    self.stop_event.wait(max(0.0, 1 / self.fps - (time.monotonic() - started)))
        except (OSError, ConnectionError) as e:
            log.warning("case lighting stopped: %s (is OpenRGB running with its SDK server on?)", e)

    def _take_over(self, o: OpenRGB) -> None:
        self.devices = []
        for c in o.controllers():
            if c.kind in SKIP_KINDS or not c.led_count:
                continue
            if "Direct" in c.modes:
                o.set_mode(c, "Direct")
            else:
                o.set_custom_mode(c.index)
            self.devices.append((c, key_strip(c.led_count)))
        log.info("case lighting on %s", ", ".join(c.name for c, _ in self.devices))

    def stop(self) -> None:
        self.stop_event.set()


def idle_seconds() -> float | None:
    """How long the user has not touched any keyboard or mouse, from GNOME (None if unknown)."""
    try:
        from gi.repository import Gio

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core",
                              "org.gnome.Mutter.IdleMonitor", "GetIdletime", None, None, Gio.DBusCallFlags.NONE, 1000, None)
        return reply.unpack()[0] / 1000
    except Exception:  # not GNOME, no session bus
        return None
