"""Lighting designs: the one model behind the picture, live tries, host mode and stored animations.

A design is a stack of layers. Each layer is a set of keys and one effect; a later layer wins
where layers overlap, and keys in no layer stay dark. The same ``Design.frame(t)`` feeds the
on-screen keyboard, the keyboard itself (live or from the daemon) and the frames uploaded to
the brightness + 9 slot, so what you see is what the keyboard plays.

Effects (``Layer.effect``):

* ``static``: one colour.
* ``sequence``: steps (or fades) through the colours; with a direction the steps travel across
  the keys as a wave.
* ``ripple``: the colours stay in place as stripes across the keys (in the layer's direction)
  while a ripple of brightness runs over them, like wind through a flag.
* ``rainbow``: all hues, moving across the keys in the layer's direction.
* ``breathing``: the first colour fades in and out.
* ``pinwheel``: the colours as sectors turning around the keys' centre.
* ``comet``: a bright head runs round the keys' centre, its tail cooling through the colours.
* ``turning``: the stripes rotate, from vertical through horizontal and back.
* ``sparkle``: dim stripes with keys flashing in the colours at random (the same every loop).
* ``heartbeat``: the stripes beat twice, then rest.
* ``raise``: the stripes rise row by row from the bottom, wave, then fade.
"""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass, field

from . import layout

EFFECTS = {
    "static": "Static colour",
    "sequence": "Colour sequence",
    "ripple": "Ripple (stripes that wave)",
    "rainbow": "Rainbow",
    "breathing": "Breathing",
    "pinwheel": "Pinwheel (turning sectors)",
    "comet": "Comet (runs in a circle)",
    "turning": "Turning stripes",
    "sparkle": "Sparkles",
    "heartbeat": "Heartbeat",
    "raise": "Raise the flag",
    "flags": "Flags (whole keyboard, in turn)",
}
STRIPED = {"ripple", "sparkle", "heartbeat", "raise"}  # effects that lay the colours out as stripes
DIRECTIONS = {
    "none": "Everywhere at once",
    "right": "Left to right",
    "left": "Right to left",
    "down": "Top to bottom",
    "up": "Bottom to top",
    "out": "From the centre out",
}


def _rgb(c: int) -> tuple[float, float, float]:
    return ((c >> 16) & 0xFF) / 255, ((c >> 8) & 0xFF) / 255, (c & 0xFF) / 255


def _pack(r: float, g: float, b: float) -> int:
    clip = lambda v: max(0, min(255, round(v * 255)))  # noqa: E731
    return (clip(r) << 16) | (clip(g) << 8) | clip(b)


def scale(c: int, k: float) -> int:
    r, g, b = _rgb(c)
    return _pack(r * k, g * k, b * k)


def lerp(a: int, b: int, t: float) -> int:
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return _pack(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


@dataclass
class Layer:
    name: str
    keys: list[str]  # key names from layout (A, F1, Insert, Logo, Play/Pause, ...)
    effect: str = "static"
    colors: list[int] = field(default_factory=lambda: [0xFFFFFF])
    period_ms: int = 2000  # one full cycle of the effect
    direction: str = "none"
    fade: bool = True  # sequence: blend between colours
    floor: float = 0.3  # ripple/breathing: dimmest level, 0-1
    spread: float = 0.5  # sequence/ripple/rainbow: how far apart in the cycle the first and last keys are
    background: int | None = None  # raise/sparkle: what keys show where the effect isn't lit (None = dark)
    flags: list[str] = field(default_factory=list)  # flags effect: the flags shown in turn (keys of FLAGS)

    def to_dict(self) -> dict:
        return {"name": self.name, "keys": list(self.keys), "effect": self.effect, "colors": [int(c) for c in self.colors],
                "period_ms": self.period_ms, "direction": self.direction, "fade": self.fade, "floor": self.floor,
                "spread": self.spread, "background": self.background, "flags": list(self.flags)}

    @classmethod
    def from_dict(cls, d: dict) -> "Layer":
        return cls(name=d.get("name", "Layer"), keys=list(d.get("keys", [])), effect=d.get("effect", "static"),
                   colors=[int(c) for c in d.get("colors", [0xFFFFFF])] or [0xFFFFFF],
                   period_ms=int(d.get("period_ms", 2000)), direction=d.get("direction", "none"),
                   fade=bool(d.get("fade", True)), floor=float(d.get("floor", 0.3)), spread=float(d.get("spread", 0.5)),
                   background=None if d.get("background") is None else int(d["background"]),
                   flags=list(d.get("flags", [])))

    @property
    def animated(self) -> bool:
        return self.effect != "static" and not (self.effect == "sequence" and len(self.colors) < 2)

    @property
    def cycle_ms(self) -> int:
        """Time until the layer repeats: one period, or one period per flag for the flags effect."""
        n = max(1, len(self.flags)) if self.effect == "flags" else 1
        return max(50, self.period_ms) * n


@dataclass
class Design:
    name: str
    layers: list[Layer] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "layers": [layer.to_dict() for layer in self.layers]}

    @classmethod
    def from_dict(cls, d: dict) -> "Design":
        return cls(name=d.get("name", "Design"), layers=[Layer.from_dict(x) for x in d.get("layers", [])])

    @property
    def animated(self) -> bool:
        return any(layer.animated for layer in self.layers)

    def loop_ms(self, limit: int = 20000) -> int:
        """A length after which every layer is back where it started (or ``limit``, if that is shorter)."""
        periods = [layer.cycle_ms for layer in self.layers if layer.animated]
        if not periods:
            return 0
        loop = 1
        for p in periods:
            loop = loop * p // math.gcd(loop, p)
            if loop > limit:
                return max(periods)
        return loop

    def renderer(self, zones: list[int] | None = None) -> "DesignRenderer":
        return DesignRenderer(self, zones)


def _positions(caps: list[layout.KeyCap]) -> dict[str, tuple[float, float]]:
    """Each key's centre, normalised to 0-1 within the given keys."""
    if not caps:
        return {}
    x0 = min(c.cx for c in caps)
    x1 = max(c.cx for c in caps)
    y0 = min(c.y + c.h / 2 for c in caps)
    y1 = max(c.y + c.h / 2 for c in caps)
    return {c.name: ((c.cx - x0) / (x1 - x0) if x1 > x0 else 0.5, (c.y + c.h / 2 - y0) / (y1 - y0) if y1 > y0 else 0.5)
            for c in caps}


def _along(direction: str, fx: float, fy: float) -> float:
    """Position 0-1 of a key along a direction."""
    return {"right": fx, "left": 1 - fx, "down": fy, "up": 1 - fy,
            "out": min(1.0, math.hypot(fx - 0.5, (fy - 0.5) * 0.4) * 2)}.get(direction, 0.0)


def _stripe(layer: "Layer", colors: list[int], fx: float, fy: float) -> int:
    d = _along(layer.direction if layer.direction != "none" else "right", fx, fy)
    return colors[min(len(colors) - 1, int(d * len(colors)))]


def _noise(zone: int, slot: int) -> float:
    """A repeatable pseudo-random number 0-1 for a key and a moment."""
    h = (zone * 73856093) ^ (slot * 19349663) ^ 0x5BD1E995
    h = (h * 2654435761) & 0xFFFFFFFF
    return (h >> 8) / float(1 << 24)


def _shape_effect(layer: "Layer", colors: list[int], zone: int, fx: float, fy: float, cycle: float) -> int:
    k = len(colors)
    phase = cycle % 1.0
    angle = (math.atan2(fy - 0.5, fx - 0.5) / (2 * math.pi)) % 1.0
    if layer.effect == "pinwheel":
        return colors[int(((angle - phase) % 1.0) * k) % k]
    if layer.effect == "comet":
        behind = (phase - angle) % 1.0  # 0 at the head
        length = max(0.05, layer.spread)
        if behind >= length:
            return scale(colors[-1], layer.floor * 0.5)
        part = behind / length
        return scale(colors[min(k - 1, int(part * k))], layer.floor + (1 - layer.floor) * (1 - part))
    if layer.effect == "turning":
        th = 2 * math.pi * phase
        u = ((fx - 0.5) * math.cos(th) + (fy - 0.5) * math.sin(th)) / math.sqrt(0.5)  # -1..1
        return colors[min(k - 1, int((u + 1) / 2 * k))]
    base = _stripe(layer, colors, fx, fy)
    if layer.effect == "sparkle":
        slots = 10  # moments per loop
        slot = int(phase * slots)
        local = phase * slots - slot
        dim = scale(base, layer.floor) if layer.background is None else layer.background
        if _noise(zone, slot) < max(0.05, layer.spread):
            flash = colors[int(_noise(zone + 997, slot) * k) % k]
            return lerp(dim, flash, (1 - local) ** 2)
        return dim
    if layer.effect == "heartbeat":
        beat = lambda t0: math.exp(-max(0.0, phase - t0) * 18) if phase >= t0 else 0.0  # noqa: E731
        level = max(beat(0.0), 0.8 * beat(0.22))
        return scale(base, layer.floor + (1 - layer.floor) * level)
    if layer.effect == "raise":
        height = 1 - fy  # 0 at the bottom row
        # the sky behind the flag: the background colour, paler towards the top
        sky = 0 if layer.background is None else lerp(layer.background, 0xFFFFFF, (1 - fy) * 0.55)
        wave = 0.5 + 0.5 * math.sin(2 * math.pi * (phase * 3 - fx * 0.5))
        flag = scale(base, layer.floor + (1 - layer.floor) * wave)
        if phase < 0.45:  # rising: each row fades in as the top edge of the flag passes it
            soft = 0.3
            edge = phase / 0.45 * (1 + soft)
            return lerp(sky, flag, min(1.0, max(0.0, (edge - height) / soft)))
        if phase > 0.85:  # fading back into the sky
            return lerp(flag, sky, (phase - 0.85) / 0.15)
        return flag
    return base


# Flags as colour at a position (fx, fy from 0 to 1 across the keys, y down). LED-friendly colours.
FLAG_RED, FLAG_WHITE = 0xFF0010, 0xFFFFFF
# A maple leaf in a unit square (y down), simplified from the Canadian flag
MAPLE_LEAF = [(0.50, 0.00), (0.59, 0.17), (0.71, 0.11), (0.66, 0.40), (0.85, 0.22), (0.81, 0.34), (0.99, 0.37),
              (0.87, 0.51), (0.91, 0.59), (0.61, 0.70), (0.65, 0.80), (0.53, 0.77), (0.54, 1.00), (0.46, 1.00),
              (0.47, 0.77), (0.35, 0.80), (0.39, 0.70), (0.09, 0.59), (0.13, 0.51), (0.01, 0.37), (0.19, 0.34),
              (0.15, 0.22), (0.34, 0.40), (0.29, 0.11), (0.41, 0.17)]


def _inside(x: float, y: float, poly) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _romania(fx: float, fy: float) -> int:
    return (0x0030FF, 0xFFC800, 0xFF0010)[min(2, int(fx * 3))]


def _canada(fx: float, fy: float) -> int:
    if fx < 0.25 or fx > 0.75:
        return FLAG_RED
    # the leaf fills most of the white square in the middle; the keyboard is about 2.8 times wider than tall
    lx, ly = (fx - 0.33) / 0.34, (fy - 0.04) / 0.94
    return FLAG_RED if 0 <= lx <= 1 and 0 <= ly <= 1 and _inside(lx, ly, MAPLE_LEAF) else FLAG_WHITE


FLAGS = {"romania": ("Romania", _romania), "canada": ("Canada", _canada)}


def _flags_effect(layer: "Layer", fx: float, fy: float, t: float) -> int:
    """Each flag fills the keys and ripples for one period, then the next wipes in from the left."""
    flags = [FLAGS[f][1] for f in layer.flags if f in FLAGS] or [_romania]
    per = max(50, layer.period_ms) / 1000
    pos = (t / per) % len(flags)
    i = int(pos)
    within = pos - i
    wipe = 0.18  # last part of each flag's time: the next one sweeps in
    current = flags[i]
    if within > 1 - wipe and fx < (within - (1 - wipe)) / wipe * 1.05:
        current = flags[(i + 1) % len(flags)]
    # brightness ripple, one wave per flag, in a few steps so whole columns change together (this keeps
    # the animation small enough to store on the keyboard)
    wave = 0.5 + 0.5 * math.sin(2 * math.pi * (t / per - fx * layer.spread))
    steps = 4
    level = layer.floor + (1 - layer.floor) * round(wave * (steps - 1)) / (steps - 1)
    return scale(current(fx, fy), level)


class DesignRenderer:
    """Colours of every key of a design at a point in time."""

    def __init__(self, design: Design, zones: list[int] | None = None):
        self.design = design
        caps = layout.keycaps(zones)
        by_name = {c.name: c for c in caps}
        self.layers = []
        for layer in design.layers:
            mine = [by_name[k] for k in layer.keys if k in by_name]
            pos = _positions(mine)
            self.layers.append((layer, [(c.zone, *pos[c.name]) for c in mine]))
        self.zones = [c.zone for c in caps]

    def frame(self, t: float) -> dict[int, int]:
        """``t`` in seconds."""
        out = {z: 0 for z in self.zones}
        for layer, keys in self.layers:
            period = max(50, layer.period_ms) / 1000
            cycle = t / period
            colors = layer.colors or [0xFFFFFF]
            for zone, fx, fy in keys:
                d = _along(layer.direction, fx, fy)
                if layer.effect == "static":
                    c = colors[0]
                elif layer.effect == "sequence":
                    n = len(colors)
                    phase = (cycle - d * layer.spread) * n
                    i = math.floor(phase)
                    c = colors[i % n] if not layer.fade or n == 1 else lerp(colors[i % n], colors[(i + 1) % n], phase - i)
                elif layer.effect == "ripple":
                    stripe = colors[min(len(colors) - 1, int(d * len(colors)))] if layer.direction != "none" else colors[0]
                    across = fy if layer.direction in ("right", "left") else fx  # the free edge lags a little
                    wave = 0.5 + 0.5 * math.sin(2 * math.pi * (cycle - d * layer.spread - across * 0.1))
                    c = scale(stripe, layer.floor + (1 - layer.floor) * wave ** 1.5)
                elif layer.effect == "rainbow":
                    r, g, b = colorsys.hsv_to_rgb((cycle - d * layer.spread) % 1.0, 1.0, 1.0)
                    c = _pack(r, g, b)
                elif layer.effect == "breathing":
                    level = 0.5 - 0.5 * math.cos(2 * math.pi * cycle)
                    c = scale(colors[0], layer.floor + (1 - layer.floor) * level)
                elif layer.effect == "flags":
                    c = _flags_effect(layer, fx, fy, t)
                elif layer.effect in ("pinwheel", "comet", "turning", "sparkle", "heartbeat", "raise"):
                    c = _shape_effect(layer, colors, zone, fx, fy, cycle)
                else:
                    c = colors[0]
                out[zone] = c
        return out

    def frames(self, period_ms: int) -> list[tuple[dict[int, int], int]]:
        """The design sampled every ``period_ms`` over one loop, merging repeated frames: [(colours, periods)]."""
        loop = self.design.loop_ms()
        if not loop:
            return [(self.frame(0.0), 1)]
        out: list[tuple[dict[int, int], int]] = []
        for i in range(max(1, round(loop / period_ms))):
            colors = self.frame(i * period_ms / 1000)
            if out and out[-1][0] == colors:
                out[-1] = (colors, out[-1][1] + 1)
            else:
                out.append((colors, 1))
        return out


def fit_frames(design: Design, zones: list[int], periods=(48, 50, 60, 72, 75, 80, 90, 96, 100, 120, 144, 150, 160, 180, 200, 240),
               max_size: int | None = None):
    """Frames for the keyboard's custom-animation slot: (frames, frame period ms, file bytes).

    Tries the smoothest frame period first and slows down until the animation fits. Raises
    ValueError if it doesn't fit even at the slowest.
    """
    from .features import rgb_store

    r = design.renderer(zones)
    last = None
    for p in periods:
        frames = r.frames(p)
        try:
            return frames, p, rgb_store.build_file(design.name[:31], frames, period_ms=p,
                                                   max_size=max_size or rgb_store.FILE_SIZE)
        except ValueError as e:
            last = e
    raise ValueError(f"“{design.name}” has too much movement to store on the keyboard ({last}); "
                     "use fewer animated keys or slower effects")


def from_lighting(light: dict, name: str = "My design") -> Design:
    """Turn an older lighting setup (painted keys, one colour sequence) into a design."""
    names = {layout.zone_of(c.name): c.name for c in layout.keycaps()}
    painted: dict[int, list[str]] = {}
    for z, c in (light.get("keys") or {}).items():
        if int(z) in names:
            painted.setdefault(int(c), []).append(names[int(z)])
    layers = []
    anim = light.get("animation") or {}
    if anim.get("enabled") and anim.get("colors"):
        keep = anim.get("keep_painted", True)
        taken = {k for ks in painted.values() for k in ks} if keep else set()
        direction = {"wave_right": "right", "wave_left": "left", "wave_down": "down", "wave_up": "up",
                     "center_out": "out"}.get(anim.get("pattern", "all"), "none")
        colors = [int(c) for c in anim["colors"]]
        layers.append(Layer("Colour sequence", [k for k in names.values() if k not in taken], "sequence", colors,
                            period_ms=max(50, int(anim.get("step_ms", 1000))) * len(colors), direction=direction,
                            fade=bool(anim.get("fade", True)), spread=1.0))
        if not keep:
            painted = {}
    for color, keys in painted.items():
        layers.append(Layer(f"Painted #{color:06x}", sorted(keys), "static", [color]))
    return Design(name, layers)
