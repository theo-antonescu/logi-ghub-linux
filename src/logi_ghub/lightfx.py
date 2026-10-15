"""Lighting state: one renderer used both for the on-screen preview and for driving the keyboard.

A profile's ``lighting`` dict looks like::

    {
      "zones": {"0": {"effect": 1, "color": 0x00ffff}, "1": {...}},   # firmware effects per cluster
      "keys": {"38": 0xff0000, ...},                                     # per-key paint (zone id -> rgb)
      "brightness": 60,
      "animation": {                                                     # host-driven colour sequence
        "enabled": true, "colors": [0xff0000, 0x00ff00, 0x0000ff],
        "step_ms": 1000, "fade": true, "pattern": "all", "keep_painted": true
      }
    }

Firmware effects (static, breathing, cycle, wave, ripple) run on the keyboard itself; the
renderer only imitates them for the preview. Colour sequences run on the computer: the
:class:`Animator` sends per-key frames while the app or the logi-ghubd service is running.
"""

from __future__ import annotations

import colorsys
import json
import logging
import math
import threading
import time

from . import layout
from .config import data_dir
from .features import lighting

log = logging.getLogger(__name__)

PATTERNS = {
    "all": "Whole keyboard",
    "wave_right": "Wave to the right",
    "wave_left": "Wave to the left",
    "wave_down": "Wave downwards",
    "wave_up": "Wave upwards",
    "center_out": "From the centre outwards",
}
LOGO_ZONE = lighting.SPECIAL_ZONES["Logo"]


def default_animation() -> dict:
    return {"enabled": False, "colors": [0xFF0000, 0x00FF00, 0x0000FF], "step_ms": 1000, "fade": True,
            "pattern": "all", "keep_painted": True}


def _rgb(c: int) -> tuple[float, float, float]:
    return ((c >> 16) & 0xFF) / 255, ((c >> 8) & 0xFF) / 255, (c & 0xFF) / 255


def _pack(r: float, g: float, b: float) -> int:
    return (round(max(0, min(1, r)) * 255) << 16) | (round(max(0, min(1, g)) * 255) << 8) | round(max(0, min(1, b)) * 255)


def lerp(a: int, b: int, t: float) -> int:
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return _pack(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


def scale(c: int, k: float) -> int:
    r, g, b = _rgb(c)
    return _pack(r * k, g * k, b * k)


def hue(h: float) -> int:
    return _pack(*colorsys.hsv_to_rgb(h % 1.0, 1.0, 1.0))


def sequence_color(colors: list[int], phase: float, fade: bool) -> int:
    """Colour at ``phase`` (in steps) of a looping sequence."""
    n = len(colors)
    if n == 0:
        return 0
    i = math.floor(phase)
    if not fade or n == 1:
        return colors[i % n]
    return lerp(colors[i % n], colors[(i + 1) % n], phase - i)


class Renderer:
    """Computes the colour of every key at a point in time."""

    def __init__(self, light: dict, zones: list[int] | None = None, clusters: int = 2):
        self.light = light or {}
        self.caps = layout.keycaps(zones)
        x0, y0, x1, y1 = layout.bounds(self.caps) if self.caps else (0, 0, 1, 1)
        main = [c for c in self.caps if c.y >= 0] or self.caps
        mx0, my0, mx1, my1 = layout.bounds(main) if main else (0, 0, 1, 1)
        self.pos: dict[int, tuple[float, float]] = {}
        for c in self.caps:
            fx = (c.cx - mx0) / max(1e-6, mx1 - mx0)
            fy = (c.y + 0.5 - my0) / max(1e-6, my1 - my0)
            self.pos[c.zone] = (min(1.0, max(0.0, fx)), min(1.0, max(0.0, fy)))
        self.clusters = clusters

    @property
    def animation(self) -> dict | None:
        a = self.light.get("animation")
        return a if a and a.get("enabled") and a.get("colors") else None

    def painted(self) -> dict[int, int]:
        return {int(k): int(v) for k, v in (self.light.get("keys") or {}).items()}

    def zone_effect(self, cluster: int) -> lighting.Effect | None:
        d = (self.light.get("zones") or {}).get(str(cluster))
        return lighting.Effect.from_dict(d) if d else None

    def is_animated(self) -> bool:
        if self.animation:
            return True
        for c in range(self.clusters):
            e = self.zone_effect(c)
            if e and e.effect_id not in (0x00, 0x01, 0x0B, 0x17):
                return True
        return False

    def _offset(self, zone: int, pattern: str, n: int) -> float:
        fx, fy = self.pos.get(zone, (0.5, 0.5))
        if pattern == "wave_right":
            return -fx * n
        if pattern == "wave_left":
            return -(1 - fx) * n
        if pattern == "wave_down":
            return -fy * n
        if pattern == "wave_up":
            return -(1 - fy) * n
        if pattern == "center_out":
            return -math.hypot(fx - 0.5, (fy - 0.5) * 0.4) * 2 * n
        return 0.0

    def _firmware(self, zone: int, t: float) -> int:
        cluster = 1 if zone == LOGO_ZONE and self.clusters > 1 else 0
        e = self.zone_effect(cluster)
        if e is None:
            return 0x202020  # unknown: whatever the keyboard's own lighting is doing
        eid = e.effect_id
        if eid == 0x00:
            return 0
        if eid in (0x01, 0x0B, 0x17):
            return e.color
        period = max(100, e.period) / 1000
        if eid == 0x0A:  # breathing
            level = 0.5 - 0.5 * math.cos(2 * math.pi * t / period)
            return scale(e.color, level * e.intensity / 100)
        if eid in (0x03, 0x15):  # colour cycle
            return scale(hue(t / period), e.intensity / 100)
        if eid in (0x04, 0x16):  # colour wave
            fx, fy = self.pos.get(zone, (0.5, 0.5))
            d = {1: -fx, 6: fx - 1, 2: -fy, 7: fy - 1, 3: -abs(fx - 0.5), 8: abs(fx - 0.5)}.get(e.direction, -fx)
            return scale(hue(t / period + d), e.intensity / 100)
        return e.color

    def frame(self, t: float) -> dict[int, int]:
        out: dict[int, int] = {}
        painted = self.painted()
        anim = self.animation
        if anim:
            colors = [int(c) for c in anim["colors"]]
            step = max(50, int(anim.get("step_ms", 1000))) / 1000
            n = len(colors)
            pattern = anim.get("pattern", "all")
            for c in self.caps:
                if anim.get("keep_painted", True) and c.zone in painted:
                    out[c.zone] = painted[c.zone]
                else:
                    out[c.zone] = sequence_color(colors, t / step + self._offset(c.zone, pattern, n), anim.get("fade", True))
            return out
        static0 = (e := self.zone_effect(0)) is not None and e.effect_id == 0x01
        for c in self.caps:
            if static0 and c.zone in painted and c.zone != LOGO_ZONE:
                out[c.zone] = painted[c.zone]
            elif c.zone == LOGO_ZONE and c.zone in painted and (self.zone_effect(1) is None or self.zone_effect(1).effect_id == 0x01):
                out[c.zone] = painted[c.zone]
            else:
                out[c.zone] = self._firmware(c.zone, t)
        return out


def _quantize(colors: dict[int, int], bits: int = 4) -> dict[int, int]:
    """Round colour channels so keys with nearly equal colours share one report and tiny changes are skipped."""
    step = 1 << (8 - bits)

    def q(ch: int) -> int:
        return min(255, round(ch / step) * step)

    return {z: (q(c >> 16 & 0xFF) << 16) | (q(c >> 8 & 0xFF) << 8) | q(c & 0xFF) for z, c in colors.items()}


class Animator(threading.Thread):
    """Plays a colour sequence on the keyboard by sending per-key frames."""

    def __init__(self, dev, renderer, on_error=None, fps: float | None = None, quant_bits: int | None = None):
        super().__init__(name="lighting-animator", daemon=True)
        self.dev = dev
        self.renderer = renderer  # anything with frame(t) -> {zone: colour}
        self.stop_event = threading.Event()
        self.paused = threading.Event()
        self.on_error = on_error
        anim = getattr(renderer, "animation", None) or {}
        wave = anim.get("pattern", "all") != "all"
        self.fps = fps or (8 if wave else 15)
        # Rounding only matters while colours fade; stepped sequences keep the exact colours chosen.
        self.quant_bits = quant_bits or (8 if not anim.get("fade", True) else (4 if wave else 6))
        self.last: dict[int, int] = {}

    def run(self) -> None:
        pk = self.dev.per_key
        zones = set(pk.zones)
        start = time.monotonic()
        failures = 0
        while not self.stop_event.is_set():
            t0 = time.monotonic()
            if not self.paused.is_set():
                frame = {z: c for z, c in self.renderer.frame(t0 - start).items() if z in zones}
                frame = _quantize(frame, self.quant_bits)
                changed = {z: c for z, c in frame.items() if self.last.get(z) != c}
                if changed:
                    try:
                        pk.set_colors(changed)
                        self.last.update(changed)
                        failures = 0
                    except Exception as e:  # noqa: BLE001 - device asleep, unplugged, ...
                        failures += 1
                        self.last.clear()
                        if failures >= 5:
                            log.warning("lighting animation stopped: %s", e)
                            if self.on_error:
                                self.on_error(e)
                            return
                        time.sleep(1.0)
            self.stop_event.wait(max(0.0, 1 / self.fps - (time.monotonic() - t0)))

    def stop(self) -> None:
        self.stop_event.set()


def apply(dev, light: dict, *, claim: bool = True) -> Animator | None:
    """Apply a lighting dict to ``dev``. Returns the running animator for colour sequences."""
    rgb, pk = dev.rgb, dev.per_key
    if rgb is None:
        return None
    if claim:
        rgb.claim()
    if dev.brightness and light.get("brightness") is not None:
        dev.brightness.set(int(light["brightness"]))
    if light.get("design") and pk:
        from .design import Design

        d = Design.from_dict(light["design"])
        r = d.renderer(pk.zones)
        rgb.hold_static()
        record_applied(dev, light)
        if not d.animated:
            pk.set_colors(r.frame(0.0))
            return None
        # same frames as the stored animation (48 ms apart), sent exactly
        anim = Animator(dev, r, fps=1000 / 48, quant_bits=8)
        anim.start()
        return anim
    renderer = Renderer(light, pk.zones if pk else None, len(rgb.zones))
    if renderer.animation and pk:
        rgb.hold_static()  # per-key frames show only while the firmware effect is Static
        anim = Animator(dev, renderer)
        anim.start()
        record_applied(dev, light)
        return anim
    for z, eff in (light.get("zones") or {}).items():
        try:
            rgb.set_effect(int(z), lighting.Effect.from_dict(eff))
        except ValueError as e:
            log.warning("zone %s: %s", z, e)
    if pk and light.get("keys"):
        rgb.hold_static(skip={int(z) for z in light.get("zones") or {}})
        frame = renderer.frame(0.0)
        pk.set_colors({z: c for z, c in frame.items() if z in pk.zones})
    record_applied(dev, light)
    return None


# What this program last put on each device, so the app can show it again later.
def _applied_path(model_key: str):
    d = data_dir() / "applied"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{model_key.replace(':', '-')}.json"


def record_applied(dev, light: dict) -> None:
    try:
        _applied_path(dev.key).write_text(json.dumps({"time": time.time(), "lighting": light}))
    except OSError as e:
        log.debug("could not record applied lighting: %s", e)


def last_applied(model_key: str) -> dict | None:
    try:
        return json.loads(_applied_path(model_key).read_text())["lighting"]
    except (OSError, ValueError, KeyError):
        return None


def clear_applied(model_key: str) -> None:
    _applied_path(model_key).unlink(missing_ok=True)
