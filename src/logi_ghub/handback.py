"""Handing devices back to their on-board profiles after host mode.

A wireless device that is asleep when the daemon lets go of it can't be
told, and it stays in host mode (diverted buttons, the host's lighting)
after it wakes. The daemon remembers such devices here and hands them back
the next time it sees them online.
"""

from __future__ import annotations

import json
import logging

from .config import data_dir
from .features import lighting
from .features import onboard as ob
from .hidpp import DeviceOffline, HIDPPError

log = logging.getLogger(__name__)


def _path():
    return data_dir() / "handback-pending.json"


def pending() -> set[str]:
    try:
        return set(json.loads(_path().read_text()))
    except (OSError, ValueError):
        return set()


def _save(keys: set[str]) -> None:
    try:
        if keys:
            _path().write_text(json.dumps(sorted(keys)))
        else:
            _path().unlink(missing_ok=True)
    except OSError as e:
        log.warning("could not record devices still in host mode: %s", e)


def mark(key: str) -> None:
    _save(pending() | {key})


def clear(key: str) -> None:
    keys = pending()
    if key in keys:
        _save(keys - {key})


def hand_back(dev) -> bool:
    """Undo everything host mode may have changed on ``dev``. Returns False if the device didn't answer."""
    try:
        spy = dev.button_spy
        if spy:
            spy.stop()
            spy.set_remapping(spy.identity())
        if dev.gkeys:
            dev.gkeys.enable_software_control(False)
        rgb = dev.rgb
        if isinstance(rgb, lighting.RGBEffects) and rgb.get_sw_control() != (0, 0):
            rgb.power(True)
            rgb.release()
        elif dev.onboard:
            dev.onboard.set_mode(ob.MODE_ONBOARD)
        return True
    except (HIDPPError, DeviceOffline, TimeoutError, OSError) as e:
        log.info("%s: could not hand back yet (%s)", dev.name, e)
        return False
