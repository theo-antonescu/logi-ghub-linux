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
}
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

    def to_dict(self) -> dict:
        return {"name": self.name, "keys": list(self.keys), "effect": self.effect, "colors": [int(c) for c in self.colors],
                "period_ms": self.period_ms, "direction": self.direction, "fade": self.fade, "floor": self.floor,
                "spread": self.spread}

    @classmethod
    def from_dict(cls, d: dict) -> "Layer":
        return cls(name=d.get("name", "Layer"), keys=list(d.get("keys", [])), effect=d.get("effect", "static"),
                   colors=[int(c) for c in d.get("colors", [0xFFFFFF])] or [0xFFFFFF],
                   period_ms=int(d.get("period_ms", 2000)), direction=d.get("direction", "none"),
                   fade=bool(d.get("fade", True)), floor=float(d.get("floor", 0.3)), spread=float(d.get("spread", 0.5)))

    @property
    def animated(self) -> bool:
        return self.effect != "static" and not (self.effect == "sequence" and len(self.colors) < 2)


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
        periods = [max(50, layer.period_ms) for layer in self.layers if layer.animated]
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


def fit_frames(design: Design, zones: list[int], periods=(48, 50, 60, 72, 75, 80, 90, 96, 100, 120, 144, 150, 160, 180, 200, 240)):
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
            return frames, p, rgb_store.build_file(design.name[:31], frames, period_ms=p)
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
