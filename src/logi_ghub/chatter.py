"""Blocking double presses ("key chatter") on keyboards, on this computer.

A worn or dirty switch can bounce: one press reaches the computer as two
presses a few milliseconds apart. The G915 X has no setting for this, and
nothing can be stored on it to fix it (G HUB's Lua scripts run inside G HUB
on the computer, not on the keyboard), so the filter runs in logi-ghubd's
keyboard grab (keyremap.KeyRemapper): a press that comes less than
``threshold_ms`` after the same key was let go is dropped, together with its
release, before anything sees it. A real double letter ("ll", "ee") leaves a
longer gap than a bounce.

It works whenever the keyboard is connected to this computer (receiver or
cable), with or without a host-mode profile. Over Bluetooth to another device
the keyboard's raw key presses go straight to that device.

Per-key counts of what was blocked, and how close the presses were that got
through, are kept in ``double-press-stats.json`` to show which keys chatter
and whether the threshold fits. Only counts and gaps are kept, never what was
typed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field

from . import keys
from .config import config_dir, data_dir

log = logging.getLogger(__name__)

DEFAULT_MS = 40
MAX_MS = 500


@dataclass
class Settings:
    enabled: bool = False
    threshold_ms: int = DEFAULT_MS
    keys: dict[str, int] = field(default_factory=dict)  # key name -> its own threshold (0: never block)

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "threshold_ms": self.threshold_ms, "keys": dict(sorted(self.keys.items()))}

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        return cls(enabled=bool(d.get("enabled")), threshold_ms=_ms(d.get("threshold_ms", DEFAULT_MS)),
                   keys={str(k): _ms(v) for k, v in d.get("keys", {}).items()})


def _ms(value) -> int:
    value = int(value)
    if not 0 <= value <= MAX_MS:
        raise ValueError(f"{value} ms is out of range (0 to {MAX_MS})")
    return value


# --- settings file: {device key: settings} --------------------------------------------------
def settings_path():
    return config_dir() / "double-press.json"


def settings_mtime() -> float:
    try:
        return os.stat(settings_path()).st_mtime
    except OSError:
        return 0.0


def load_all() -> dict[str, Settings]:
    try:
        raw = json.loads(settings_path().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("cannot read %s: %s", settings_path(), e)
        return {}
    out = {}
    for key, d in raw.items():
        try:
            out[key] = Settings.from_dict(d)
        except (TypeError, ValueError, AttributeError) as e:
            log.warning("%s: ignoring double-press settings for %s: %s", settings_path(), key, e)
    return out


def load(device_key: str) -> Settings:
    return load_all().get(device_key, Settings())


def save(device_key: str, settings: Settings) -> None:
    every = load_all()
    every[device_key] = settings
    _write_json(settings_path(), {k: s.to_dict() for k, s in sorted(every.items())})


def _write_json(path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


# --- statistics -------------------------------------------------------------------------------
def stats_path():
    return data_dir() / "double-press-stats.json"


def load_stats() -> dict[str, dict[str, dict]]:
    """{device key: {key name: {"blocked", "blocked_min_ms", "blocked_max_ms", "close", "close_min_ms"}}}"""
    try:
        return json.loads(stats_path().read_text())
    except (OSError, ValueError):
        return {}


def reset_stats(device_key: str) -> None:
    every = load_stats()
    if every.pop(device_key, None) is not None:
        _write_json(stats_path(), every)


class Stats:
    """Counts for one keyboard, kept across daemon restarts. Updated from the reader thread, saved from the daemon's."""

    def __init__(self, device_key: str):
        self.device_key = device_key
        self.keys: dict[str, dict] = load_stats().get(device_key, {})
        self.dirty = False
        self._lock = threading.Lock()

    def _entry(self, code: int) -> dict:
        return self.keys.setdefault(keys.EVDEV_TO_NAME.get(code, f"code:{code}"), {})

    def blocked(self, code: int, gap_ms: float) -> None:
        with self._lock:
            e = self._entry(code)
            e["blocked"] = e.get("blocked", 0) + 1
            e["blocked_min_ms"] = round(min(gap_ms, e.get("blocked_min_ms", gap_ms)), 1)
            e["blocked_max_ms"] = round(max(gap_ms, e.get("blocked_max_ms", gap_ms)), 1)
            self.dirty = True

    def close_call(self, code: int, gap_ms: float) -> None:
        """A press let through, but less than twice the threshold after the last release."""
        with self._lock:
            e = self._entry(code)
            e["close"] = e.get("close", 0) + 1
            e["close_min_ms"] = round(min(gap_ms, e.get("close_min_ms", gap_ms)), 1)
            self.dirty = True

    def save(self) -> None:
        with self._lock:
            if not self.dirty:
                return
            snapshot = json.loads(json.dumps(self.keys))
            self.dirty = False
        every = load_stats()
        every[self.device_key] = snapshot
        try:
            _write_json(stats_path(), every)
        except OSError as e:
            log.warning("cannot save %s: %s", stats_path(), e)


# --- the filter ---------------------------------------------------------------------------------
class ChatterFilter:
    """Decides, event by event, whether a key event is real. ``t`` is the event's time in seconds."""

    def __init__(self, settings: Settings, stats: Stats | None = None):
        from .keyremap import key_code

        self.default = settings.threshold_ms / 1000
        self.limits: dict[int, float] = {}
        for name, ms in settings.keys.items():
            try:
                self.limits[key_code(name)] = ms / 1000
            except ValueError as e:
                log.warning("double presses: %s", e)
        self.stats = stats
        self.released_at: dict[int, float] = {}
        self.dropped: set[int] = set()  # presses swallowed whose release is still to come

    def allow(self, code: int, value: int, t: float) -> bool:
        """value: 1 press, 0 release, 2 auto-repeat."""
        if code in self.dropped:
            if value == 0:
                self.dropped.discard(code)
            return False
        if value == 0:
            self.released_at[code] = t
        elif value == 1:
            last = self.released_at.get(code)
            limit = self.limits.get(code, self.default)
            if last is not None and limit > 0:
                gap = t - last
                if 0 <= gap < limit:
                    self.dropped.add(code)
                    if self.stats:
                        self.stats.blocked(code, gap * 1000)
                    return False
                if 0 <= gap < 2 * limit and self.stats:
                    self.stats.close_call(code, gap * 1000)
        return True
