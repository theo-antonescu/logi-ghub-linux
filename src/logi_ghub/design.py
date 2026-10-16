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
    "frames": "Frames (paint each frame)",
    "flag_stream": "Rising flags (one flag per row)",
    "ceremony": "Flag ceremony (raise, flutter, lower, fireworks)",
    "scanner": "Scanner (a bright head sweeping back and forth)",
}
# How a frame turns into the next one (G HUB's transition curves)
TRANSITIONS = {
    "cut": ("Cut", lambda p: 0.0),
    "linear": ("Linear", lambda p: p),
    "ease_in": ("Ease in", lambda p: p * p),
    "ease_out": ("Ease out", lambda p: 1 - (1 - p) ** 2),
    "ease_in_out": ("Ease in and out", lambda p: 3 * p * p - 2 * p ** 3),
    "late": ("Hold, then change", lambda p: max(0.0, (p - 0.7) / 0.3)),
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
    # frames effect: [{"colors": {key name: colour}, "ms": time shown, "transition": TRANSITIONS key}]
    frames: list[dict] = field(default_factory=list)
    cycle: str = "loop"  # frames effect: "loop" (1, 2, 3, 1, 2, 3) or "bounce" (1, 2, 3, 2, 1, 2, 3)
    detail: int = 0  # flags/ceremony: brightness steps of the flutter (0 = a few); more is smoother but bigger

    def to_dict(self) -> dict:
        return {"name": self.name, "keys": list(self.keys), "effect": self.effect, "colors": [int(c) for c in self.colors],
                "period_ms": self.period_ms, "direction": self.direction, "fade": self.fade, "floor": self.floor,
                "spread": self.spread, "background": self.background, "flags": list(self.flags),
                "frames": [{"colors": dict(f.get("colors", {})), "ms": int(f.get("ms", 370)),
                            "transition": f.get("transition", "linear")} for f in self.frames], "cycle": self.cycle,
                "detail": self.detail}

    @classmethod
    def from_dict(cls, d: dict) -> "Layer":
        return cls(name=d.get("name", "Layer"), keys=list(d.get("keys", [])), effect=d.get("effect", "static"),
                   colors=[int(c) for c in d.get("colors", [0xFFFFFF])] or [0xFFFFFF],
                   period_ms=int(d.get("period_ms", 2000)), direction=d.get("direction", "none"),
                   fade=bool(d.get("fade", True)), floor=float(d.get("floor", 0.3)), spread=float(d.get("spread", 0.5)),
                   background=None if d.get("background") is None else int(d["background"]),
                   flags=list(d.get("flags", [])),
                   frames=[{"colors": {k: int(v) for k, v in f.get("colors", {}).items()}, "ms": int(f.get("ms", 370)),
                            "transition": f.get("transition", "linear")} for f in d.get("frames", [])],
                   cycle=d.get("cycle", "loop"), detail=int(d.get("detail", 0)))

    @property
    def animated(self) -> bool:
        if self.effect == "frames":
            return len(self.frames) > 1
        return self.effect != "static" and not (self.effect == "sequence" and len(self.colors) < 2)

    @property
    def cycle_ms(self) -> int:
        """Time until the layer repeats: one period, one per flag (flags) or the frames' total (frames)."""
        if self.effect == "frames":
            return max(50, sum(ms for _c, ms, _tr in self.timeline()))
        n = max(1, len(self.flags)) if self.effect in ("flags", "flag_stream", "raise", "ceremony") else 1
        return max(50, self.period_ms) * n

    def timeline(self) -> list[tuple[dict, int, str]]:
        """frames effect: (colours, ms, transition) in playing order, with bounce unfolded."""
        seq = [(f.get("colors", {}), max(20, int(f.get("ms", 370))), f.get("transition", "linear")) for f in self.frames]
        if self.cycle == "bounce" and len(seq) > 2:
            seq = seq + seq[-2:0:-1]
        return seq

    @property
    def frame_keys(self) -> list[str]:
        return sorted({k for f in self.frames for k in f.get("colors", {})})


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


def _shape_effect(layer: "Layer", colors: list[int], zone: int, fx: float, fy: float, cycle: float, name: str = "") -> int:
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
        if layer.flags:  # a different small flag each time: Romania, then Canada (leaf on the centre key), ...
            which = layer.flags[int(cycle) % len(layer.flags)]
            base = FLAG_ROWS.get(which, FLAG_ROWS["romania"])[min(2, int(fx * 3))]
            if which == "canada" and name == "Home":
                base = FLAG_RED
        height = 1 - fy  # 0 at the bottom row
        # the sky behind the flag: the background colour, paler towards the top
        sky = 0 if layer.background is None else lerp(layer.background, 0xFFFFFF, (1 - fy) * 0.55)
        if layer.flags:  # diagonal ripples crossing it, like the big flags
            flag = scale(base, _sweep(fx, fy, (phase * 2) % 1.0, layer.floor, width=0.3, slant=1.0))  # 3 x 5 keys
        else:
            wave = 0.5 + 0.5 * math.sin(2 * math.pi * (phase * 3 - fx * 0.5))
            flag = scale(base, layer.floor + (1 - layer.floor) * wave)
        steps = (lambda k: round(k * 3) / 3) if layer.flags else (lambda k: k)
        # share of each cycle spent rising and fading; alternating flags stay up longer
        rise, fade = (0.2, 0.08) if layer.flags else (0.45, 0.15)
        if phase < rise:  # rising: each row fades in as the top edge of the flag passes it
            soft = 0.3
            edge = phase / rise * (1 + soft)
            return lerp(sky, flag, steps(min(1.0, max(0.0, (edge - height) / soft))))
        if phase > 1 - fade:  # fading back into the sky
            return lerp(flag, sky, steps((phase - (1 - fade)) / fade))
        return flag
    return base


# Flags as colour at a position (fx, fy from 0 to 1 across the keys, y down). LED-friendly colours.
FLAG_RED, FLAG_WHITE = 0xFF0010, 0xFFFFFF
def _romania(fx: float, fy: float, name: str = "") -> int:
    """Blue, yellow, red, the yellow stripe centred on the space bar (the middle of the typing area)."""
    centre, _stem = _canada_anchor()
    centre += ROMANIA_SHIFT
    if fx < centre - 1 / 6:
        return 0x0030FF
    return 0xFFC800 if fx <= centre + 1 / 6 else 0xFF0010


ROMANIA_SHIFT = 1 / 17.25  # one key to the right of the space bar's centre, so red is a column narrower


# The maple leaf, key by key (F5, 5 6 7 8, R T Y U I, F G H J, stem B): at one LED per key a
# drawn outline comes out ragged, so the leaf is spelled out.
MAPLE_LEAF_KEYS = {"F5", "5", "6", "7", "8", "R", "T", "Y", "U", "I", "F", "G", "H", "J", "B"}


def _canada(fx: float, fy: float, name: str = "") -> int:
    """Red, white, red bands centred on the space bar (the middle of the typing area), leaf in the middle."""
    centre, _stem = _canada_anchor()
    if name in MAPLE_LEAF_KEYS or abs(fx - centre) > 0.25:
        return FLAG_RED
    return FLAG_WHITE


_ANCHOR: tuple[float, float] | None = None


def _canada_anchor() -> tuple[float, float]:
    """(space bar's x, B row's y), as fractions of the whole keyboard."""
    global _ANCHOR
    if _ANCHOR is None:
        pos = _positions(layout.keycaps())
        _ANCHOR = (pos["Space"][0], pos["B"][1])
    return _ANCHOR


FLAGS = {"romania": ("Romania", _romania), "canada": ("Canada", _canada)}
# A flag squeezed into one row of three keys (left, middle, right)
FLAG_ROWS = {"romania": (0x0030FF, 0xFFC800, 0xFF0010), "canada": (FLAG_RED, FLAG_WHITE, FLAG_RED)}


def _flag_stream(layer: "Layer", fx: float, row: int, t: float) -> int:
    """Each row of keys is one flag (in turn from layer.flags) and the rows rise one row per period."""
    flags = [FLAG_ROWS[f] for f in layer.flags if f in FLAG_ROWS] or [FLAG_ROWS["romania"]]
    col = min(2, int(fx * 3))
    pos = row + t * 1000 / max(50, layer.period_ms)  # rising: what is below moves up
    i = math.floor(pos)
    a, b = flags[i % len(flags)][col], flags[(i + 1) % len(flags)][col]
    return lerp(a, b, pos - i) if layer.fade else a


def _flags_effect(layer: "Layer", fx: float, fy: float, t: float, name: str = "") -> int:
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
    # folds flutter from the pole (left) to the free end (right), nearly upright, stronger towards
    # the free end, as on a real flag in the wind
    level = _flutter(fx, fy, t, layer.floor, layer.detail or 2)
    return scale(current(fx, fy, name), level)


def _flutter(fx: float, fy: float, t: float, floor: float, steps: int = 2) -> float:
    """Brightness 0-1 of a flag fluttering in the wind (fx, fy: 0-1 across it, the pole on the left).

    Three sets of folds of different sizes and speeds run from the pole to the free end; their
    crests catch the light and their troughs fall into shadow (down to ``floor``). The folds curve a
    little and the wind comes in gusts, and they are stronger towards the free end. All periods divide
    6 s, so loops of 6, 12, 18, 24 ... s are seamless. ``steps`` brightness steps per side (more is
    smoother but needs more storage).
    """
    bend = 0.05 * math.sin(2 * math.pi * (0.8 * fy - t / 3.0))  # folds curve and sway
    u = fx - 0.08 * (1 - fy) + bend
    gust = 0.75 + 0.25 * math.sin(2 * math.pi * t / 6.0)
    wave = (0.55 * math.sin(2 * math.pi * (1.4 * u - t / 2.0))
            + 0.30 * math.sin(2 * math.pi * (2.3 * u - t / 1.2 + 0.4 + 0.2 * fy))
            + 0.15 * math.sin(2 * math.pi * (3.7 * u - t / 0.75 + 0.1)))
    s = max(-1.0, min(1.0, wave * gust * (0.3 + 0.7 * fx) * 1.3))
    s = round(s * steps) / steps
    rest = 0.88  # a flat flag; crests brighten to full, troughs darken to floor
    return rest + (1 - rest) * s if s > 0 else rest + (rest - floor) * s


def _sweep(fx: float, fy: float, phase: float, floor: float, width: float = 0.14, slant: float = 0.22,
           steps: int = 2) -> float:
    """Brightness 0-1 for a soft diagonal band dimming the keys to ``floor`` as it crosses from left to right.

    The band runs from bottom left to top right ("/"); ``slant`` is how far right its top is, as a share
    of the width (0.22 on the whole keyboard is about 60 degrees). ``phase`` runs 0-1 over one crossing
    (negative: no band). Kept to a few steps so whole diagonals change together, which keeps animations
    small enough to store.
    """
    if phase < 0:
        return 1.0
    u = fx - slant * (1 - fy)  # the band's top is further right than its bottom
    centre = -width + phase * (1 + slant + 2 * width) - slant
    d = abs(u - centre) / width
    if d >= 1:
        return 1.0
    dip = 0.5 + 0.5 * math.cos(math.pi * d)  # 1 in the middle of the band, 0 at its edges
    return 1 - (1 - floor) * round(dip * steps) / steps  # 2 steps: full, half way, floor


def _frames_effect(layer: "Layer", name: str, t: float) -> int:
    seq = layer.timeline()
    if not seq:
        return 0
    total = sum(ms for _c, ms, _tr in seq)
    pos = (t * 1000) % total
    for i, (colors, ms, transition) in enumerate(seq):
        if pos < ms or i == len(seq) - 1:
            nxt = seq[(i + 1) % len(seq)][0]
            ease = TRANSITIONS.get(transition, TRANSITIONS["linear"])[1]
            return lerp(colors.get(name, 0), nxt.get(name, 0), ease(min(1.0, pos / ms)))
        pos -= ms
    return 0


def _ceremony(layer: "Layer", colors: list[int], zone: int, fx: float, fy: float, t: float, name: str) -> int:
    """A small flag's ceremony, one per period: it is raised row by row out of the sky, flutters in the
    wind, is lowered again, and fireworks in its colours burst over the empty sky.

    With several layer.flags each period has the next one (Romania, then Canada with the leaf on the
    centre key, ...); otherwise the colours run in columns.
    """
    per = max(50, layer.period_ms) / 1000
    cycle = t / per
    phase = cycle % 1.0
    if layer.flags:
        which = layer.flags[int(cycle) % len(layer.flags)]
        base = FLAG_ROWS.get(which, FLAG_ROWS["romania"])[min(2, int(fx * 3))]
        if which == "canada" and name == "Home":
            base = FLAG_RED
        palette = list(FLAG_ROWS.get(which, FLAG_ROWS["romania"]))
    else:
        base = colors[min(len(colors) - 1, int(fx * len(colors)))]
        palette = colors
    sky = 0 if layer.background is None else lerp(layer.background, 0xFFFFFF, (1 - fy) * 0.55)
    height = 1 - fy  # 0 at the bottom row
    flag = scale(base, _flutter(fx, fy, t, layer.floor, layer.detail or 2))
    rise, lower, fireworks = 0.16, 0.72, 0.84  # phase boundaries
    soft = 0.35
    if phase < rise:  # raised: rows fill from the bottom
        edge = phase / rise * (1 + soft)
        return lerp(sky, flag, min(1.0, max(0.0, (edge - height) / soft)))
    if phase < lower:
        return flag
    if phase < fireworks:  # lowered: rows empty from the top
        edge = (phase - lower) / (fireworks - lower) * (1 + soft)
        return lerp(flag, sky, min(1.0, max(0.0, (edge - (1 - height)) / soft)))
    # fireworks: bursts in the flag's colours, each key flashing and fading at its own moment
    local = (phase - fireworks) / (1 - fireworks)
    night = math.sin(math.pi * min(1.0, local * 1.15)) ** 0.6  # the sky darkens for the fireworks, then dawns
    sky = lerp(sky, 0x000818, night * 0.9)
    bursts = 3
    b = int(local * bursts)
    within = local * bursts - b
    if _noise(zone, int(cycle) * 7 + b) < 0.45:
        flash = palette[int(_noise(zone + 501, int(cycle) * 7 + b) * len(palette)) % len(palette)]
        start = _noise(zone + 77, b) * 0.4
        if within >= start:  # a bright flash that fades back into the sky
            return lerp(flash, sky, min(1.0, (within - start) / 0.55) ** 0.7)
    return sky


def _scanner(layer: "Layer", colors: list[int], fx: float, t: float) -> int:
    """A bright head sweeping back and forth, leaving a trail that cools through the colours."""
    per = max(50, layer.period_ms) / 1000
    phase = (t / per) % 1.0
    head = phase * 2 if phase < 0.5 else 2 - phase * 2  # 0 -> 1 -> 0
    moving_right = phase < 0.5
    behind = (head - fx) if moving_right else (fx - head)
    width = max(0.1, layer.spread)
    dim = scale(colors[-1], layer.floor * 0.4)
    if behind < -0.08:  # ahead of the head
        return dim
    if behind < 0.08:  # the head itself: white hot
        return lerp(0xFFFFFF, colors[0], max(0.0, behind) / 0.08)
    part = min(1.0, (behind - 0.08) / width)
    if part >= 1:
        return dim
    k = part * (len(colors) - 1)
    i = int(k)
    c = lerp(colors[i], colors[min(i + 1, len(colors) - 1)], k - i)
    return lerp(c, dim, part ** 1.5)


class DesignRenderer:
    """Colours of every key of a design at a point in time."""

    def __init__(self, design: Design, zones: list[int] | None = None):
        self.design = design
        caps = layout.keycaps(zones)
        by_name = {c.name: c for c in caps}
        self.layers = []
        for layer in design.layers:
            names = layer.frame_keys if layer.effect == "frames" else layer.keys
            mine = [by_name[k] for k in names if k in by_name]
            pos = _positions(mine)
            self.layers.append((layer, [(c.zone, *pos[c.name]) for c in mine]))
        self.zones = [c.zone for c in caps]
        self.names = {c.zone: c.name for c in caps}
        self.rows = {c.zone: round(c.y) for c in caps}  # 0 for the F-key row, then one per row below

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
                    c = _flags_effect(layer, fx, fy, t, self.names[zone])
                elif layer.effect == "ceremony":
                    c = _ceremony(layer, colors, zone, fx, fy, t, self.names[zone])
                elif layer.effect == "scanner":
                    c = _scanner(layer, colors, fx, t)
                elif layer.effect == "flag_stream":
                    c = _flag_stream(layer, fx, self.rows[zone], t)
                elif layer.effect == "frames":
                    c = _frames_effect(layer, self.names[zone], t)
                elif layer.effect in ("pinwheel", "comet", "turning", "sparkle", "heartbeat", "raise"):
                    c = _shape_effect(layer, colors, zone, fx, fy, cycle, self.names[zone])
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


def fit_frames(design: Design, zones: list[int], periods=(32, 36, 40, 45, 48, 50, 60, 72, 75, 80, 90, 96, 100, 120, 144, 150, 160,
                                                          180, 200, 240),
               max_size: int | None = None):
    """Frames for the keyboard's custom-animation slot: (frames, frame period ms, file bytes).

    Tries the smoothest frame period first and slows down until the animation fits. Raises
    ValueError if it doesn't fit even at the slowest.
    """
    from .features import rgb_store

    r = design.renderer(zones)
    last = None
    sizes = [max_size] if max_size else [rgb_store.FILE_SIZE, rgb_store.LARGE_FILE_SIZE]
    loop = design.loop_ms()
    usable = [p for p in periods if not loop or loop % p == 0] or list(periods)  # whole frames per loop: no jump
    for p in usable:
        frames = r.frames(p)
        for size in sizes:  # a bigger file before a jerkier animation
            try:
                return frames, p, rgb_store.build_file(design.name[:31], frames, period_ms=p, max_size=size)
            except ValueError as e:
                last = e
    raise ValueError(f"“{design.name}” has too much movement to store on the keyboard ({last}); "
                     "use fewer animated keys or slower effects")


def fit_best(design: Design, zones: list[int], details=(12, 10, 8, 6, 5, 4, 3, 2), periods=(32, 36, 40, 45, 48)):
    """Like fit_frames, but for designs with flutter: the most detail that still fits smoothly.

    Tries the brightness steps of every layer that has them (most first) at fast frame periods, then
    falls back to fit_frames' slower periods with the least detail. Returns (frames, period, data, detail).
    """
    tuned = [layer for layer in design.layers if layer.effect in ("flags", "ceremony")]
    if not tuned:
        return (*fit_frames(design, zones), 0)
    loop = design.loop_ms()
    periods = [p for p in periods if not loop or loop % p == 0] or periods
    for period in periods:  # the fastest frame rate first, with as much detail as fits at it
        for d in details:
            for layer in tuned:
                layer.detail = d
            try:
                frames, p, data = fit_frames(design, zones, periods=(period,))
                return frames, p, data, d
            except ValueError:
                continue
    for layer in tuned:
        layer.detail = details[-1]
    return (*fit_frames(design, zones), details[-1])


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
