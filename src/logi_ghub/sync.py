"""Whole-computer lighting sync, run by logi-ghubd.

The settings live in ``~/.config/logi-ghub-linux/sync.json`` (the app writes them)::

    {"enabled": true, "design": {...a design...}, "off_after": 300,
     "balance": {"Kraken": [0.6, 0.7, 1.0]}, "keyboard": true}

While enabled, the design plays live on the keyboard and on every OpenRGB device from one start
time, and everything goes dark after ``off_after`` seconds without keyboard or mouse use.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from .config import config_dir
from .design import Design

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": False, "design": None, "off_after": 300, "balance": {}, "keyboard": True}


def settings_path():
    return config_dir() / "sync.json"


def load() -> dict:
    try:
        data = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **data}


def save(settings: dict) -> None:
    settings_path().write_text(json.dumps(settings, indent=2))


class SyncPlayer:
    """Owns the running sync. Call tick() about once a second from the daemon's loop."""

    def __init__(self, open_keyboard):
        self.open_keyboard = open_keyboard  # () -> LogiDevice or None; None when the keyboard is busy
        self.mtime = None
        self.settings = dict(DEFAULTS)
        self.case = None
        self.monitor = None
        self.monitor_checked = 0.0
        self.anim = None
        self.kb = None
        self.dark = False
        self.handback = threading.Event()  # set when the brightness key is pressed on the keyboard

    # -- lifecycle ------------------------------------------------------------------------------
    def tick(self) -> None:
        try:
            mtime = os.stat(settings_path()).st_mtime
        except OSError:
            mtime = None
        if mtime != self.mtime:
            self.mtime = mtime
            self.settings = load()
            self.stop()
            if self.settings["enabled"] and self.settings.get("design"):
                self.start()
        if self.handback.is_set():
            self._hand_back_keyboard()
        if self.case is not None:
            if not self.case.is_alive():  # OpenRGB went away: try again on the next settings check
                self.stop()
                self.mtime = None
                return
            self._idle()
            self._monitor()

    def start(self) -> None:
        from . import caselight, lightfx

        design = Design.from_dict(self.settings["design"])
        caselight.BALANCE.update({k: tuple(v) for k, v in self.settings.get("balance", {}).items()})
        t0 = time.monotonic()
        if self.settings.get("keyboard", True):
            self.kb = self.open_keyboard()
            if self.kb is not None:
                self.kb.hidpp.add_handler(self._keyboard_message)
                try:
                    self.anim = lightfx.apply(self.kb, {"design": design.to_dict()})
                    if self.anim is not None:
                        t0 = self.anim.start_time
                except Exception as e:  # noqa: BLE001 - asleep, unplugged: the case still syncs
                    log.warning("sync: keyboard not available (%s)", e)
                    self.kb = None
        self.case = caselight.CaseLights(design, t0)
        self.case.start()
        self.monitor_checked = 0.0
        self._monitor()
        log.info("sync: playing '%s'", design.name)

    def stop(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None
        if self.case is not None:
            self.case.stop()
            self.case = None
        if self.anim is not None:
            self.anim.stop()
            self.anim = None
        if self.kb is not None:
            try:
                if self.dark:
                    self.kb.rgb.power(True)
                self.kb.rgb.release()
            except Exception:  # noqa: BLE001
                pass
            self.kb.close()
            self.kb = None
        self.dark = False
        self.handback.clear()

    # -- keyboard -------------------------------------------------------------------------------
    def _keyboard_message(self, msg) -> None:
        """The brightness key is the only key the keyboard reports while the sync owns its lighting.
        Pressing it (as in brightness + 9) gives the keyboard back its own lighting."""
        from .hidpp import features as F

        if msg.swid == 0 and msg.function == 0 and msg.sub_id == self.kb.hidpp.features.get(F.BRIGHTNESS_CONTROL):
            self.handback.set()

    def _hand_back_keyboard(self) -> None:
        """Stop playing on the keyboard (the rest of the computer keeps syncing) and remember it."""
        self.handback.clear()
        if self.anim is not None:
            self.anim.stop()
            self.anim = None
        if self.kb is not None:
            try:
                self.kb.rgb.release()
            except Exception:  # noqa: BLE001
                pass
            self.kb.close()
            self.kb = None
        self.settings["keyboard"] = False
        save({**load(), "keyboard": False})
        self.mtime = os.stat(settings_path()).st_mtime
        log.info("sync: brightness key pressed, keyboard back on its own lighting")

    def _monitor(self) -> None:
        """Start the Alienware monitor's player when the monitor is (or comes back) on."""
        if self.monitor is not None and self.monitor.is_alive():
            return
        now = time.monotonic()
        if now - self.monitor_checked < 10:
            return
        self.monitor_checked = now
        from . import alienware

        if alienware.find() is not None:
            self.monitor = alienware.MonitorLights(self.case.design, self.case.t0, self.case.dark)
            self.monitor.start()

    # -- inactivity -------------------------------------------------------------------------------
    def _idle(self) -> None:
        from .caselight import idle_seconds

        limit = float(self.settings.get("off_after") or 0)
        idle = idle_seconds() if limit > 0 else None
        if idle is None:
            return
        away = idle >= limit
        if away and not self.dark:
            self.dark = True
            self.case.dark.set()
            if self.anim is not None:
                self.anim.paused.set()
                self.kb.rgb.power(False)
            log.info("sync: idle, all lights off")
        elif not away and self.dark:
            self.dark = False
            self.case.dark.clear()
            if self.anim is not None:
                self.kb.rgb.power(True)
                self.anim.last.clear()
                self.anim.paused.clear()
            log.info("sync: back, lights on")
