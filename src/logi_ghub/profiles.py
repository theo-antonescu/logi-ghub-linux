"""Profiles kept on the computer, and copying them to and from on-board memory.

A profile is a JSON file in ``~/.config/logi-ghub-linux/profiles/<model>/``.
It can be written into an on-board slot (then it works without this software
running) or applied in host mode by the daemon (needed for macros longer than
the device can store, lighting on devices without on-board lighting, etc.).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .actions import Action
from .config import config_dir
from .features import onboard as ob
from .macros import Macro

log = logging.getLogger(__name__)


@dataclass
class Profile:
    name: str
    device: str  # model key, e.g. 046d:409f
    dpi_stages: list[int] = field(default_factory=lambda: [400, 800, 1600, 3200])
    default_dpi: int = 1  # index into dpi_stages
    shift_dpi: int = 400  # sniper / DPI shift value
    report_rate: int = 1000  # Hz
    buttons: dict[int, Action] = field(default_factory=dict)  # binding index -> action
    gshift_buttons: dict[int, Action] = field(default_factory=dict)
    macros: list[Macro] = field(default_factory=list)
    lighting: dict = field(default_factory=dict)  # {"zones": {idx: effect dict}, "keys": {zone: rgb}, "brightness": n}

    # -- json -----------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "device": self.device,
            "dpi_stages": self.dpi_stages,
            "default_dpi": self.default_dpi,
            "shift_dpi": self.shift_dpi,
            "report_rate": self.report_rate,
            "buttons": {str(k): v.to_dict() for k, v in sorted(self.buttons.items())},
            "gshift_buttons": {str(k): v.to_dict() for k, v in sorted(self.gshift_buttons.items())},
            "macros": [m.to_dict() for m in self.macros],
            "lighting": self.lighting,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(
            name=d["name"],
            device=d["device"],
            dpi_stages=list(d.get("dpi_stages", [800, 1600, 3200])),
            default_dpi=int(d.get("default_dpi", 0)),
            shift_dpi=int(d.get("shift_dpi", 400)),
            report_rate=int(d.get("report_rate", 1000)),
            buttons={int(k): Action.from_dict(v) for k, v in d.get("buttons", {}).items()},
            gshift_buttons={int(k): Action.from_dict(v) for k, v in d.get("gshift_buttons", {}).items()},
            macros=[Macro.from_dict(m) for m in d.get("macros", [])],
            lighting=d.get("lighting", {}),
        )

    def macro(self, name: str) -> Macro | None:
        return next((m for m in self.macros if m.name == name), None)

    # -- conversion from on-board memory ---------------------------------------
    @classmethod
    def from_onboard(cls, model_key: str, p: ob.OnboardProfile, slots: int, memory: ob.OnboardProfiles | None = None,
                     name: str | None = None) -> "Profile":
        dpis = p.active_dpis
        shift = p.shift_dpi
        prof = cls(
            name=name or p.name or f"On-board {p.sector}",
            device=model_key,
            dpi_stages=dpis or [800],
            default_dpi=min(p.default_dpi_index, max(0, len(dpis) - 1)),
            shift_dpi=400 if shift is None else shift,
            report_rate=1000 // max(1, p.report_rate_ms),
        )
        if p.led is not None:
            prof.lighting["led"] = p.led
        for gshift, target in ((False, prof.buttons), (True, prof.gshift_buttons)):
            for i in range(slots):
                a = p.button(i, gshift)
                if a.kind == "macro" and memory is not None:
                    try:
                        ops = memory.read_macro(a.sector, a.address)
                        mname = f"Macro {a.sector}:{a.address}"
                        if not prof.macro(mname):
                            prof.macros.append(Macro.from_onboard_ops(mname, ops))
                        a = Action.run_macro(mname)
                    except Exception as e:
                        log.warning("could not read on-board macro at %s:%s: %s", a.sector, a.address, e)
                target[i] = a
        return prof

    # -- conversion to on-board memory -----------------------------------------
    def to_onboard(self, base: ob.OnboardProfile, slots: int) -> ob.OnboardProfile:
        """Overlay this profile on ``base`` (keeps lighting and unknown bytes). Macros must be placed first."""
        p = base.copy()
        stages = [int(d) for d in self.dpi_stages][: p.max_dpi_stages]
        if not stages:
            raise ValueError("profile needs at least one DPI stage")
        p.dpis = stages
        p.default_dpi_index = min(self.default_dpi, len(stages) - 1)
        p.set_shift_dpi(self.shift_dpi)
        p.report_rate_ms = max(1, round(1000 / self.report_rate))
        led = self.lighting.get("led")
        if led and p.led is not None:
            p.set_led(int(led["effect"]), int(led["color"]), int(led.get("duration", 4)), led.get("gshift_color"))
        p.name = self.name[:24]
        for gshift, source in ((False, self.buttons), (True, self.gshift_buttons)):
            for i, action in source.items():
                if i >= slots:
                    continue
                if action.kind == "macro" and not action.sector:
                    raise ValueError(f"macro {action.macro!r} has not been placed in on-board memory")
                p.set_button(i, action, gshift)
        return p


def write_to_onboard(memory: ob.OnboardProfiles, profile: Profile, slot: int, slots: int, *, enable: bool = True) -> list[str]:
    """Write ``profile`` into on-board ``slot``: macros first, then the profile, then the directory.

    Returns a list of human readable change descriptions. Callers should take a
    backup (:func:`logi_ghub.features.onboard.save_backup`) before calling this.
    """
    changes: list[str] = []
    current = memory.read_profiles()
    base = current[slot - 1]
    others = [p for p in current if p.sector != slot]
    free = memory.free_macro_sectors(others)
    placed: dict[str, tuple[int, int]] = {}
    for layer in (profile.buttons, profile.gshift_buttons):
        for i, action in list(layer.items()):
            if action.kind != "macro" or not action.macro:
                continue
            if action.macro not in placed:
                macro = profile.macro(action.macro)
                if macro is None:
                    raise ValueError(f"button {i + 1} uses unknown macro {action.macro!r}")
                if not memory.info.supports_macros:
                    raise ValueError("this mouse has no on-board macro memory")
                if macro.needs_host:
                    raise ValueError(f"macro '{macro.name}' repeats or holds keys, which on-board memory can't do; "
                                     "run this profile in host mode instead")
                if not free:
                    raise ValueError("no free on-board sectors left for macros")
                sector = free.pop(0)
                memory.write_macro(sector, macro.to_onboard_ops())
                placed[action.macro] = (sector, 0)
                changes.append(f"macro '{macro.name}' -> sector {sector}")
            sector, address = placed[action.macro]
            layer[i] = Action("macro", macro=action.macro, sector=sector, address=address)
    new = profile.to_onboard(base, slots)
    new.sector = slot
    if memory.write_profile(new):
        changes.append(f"profile slot {slot} rewritten")
    if enable:
        directory = dict(memory.read_directory())
        if not directory.get(slot):
            memory.set_enabled(slot, True)
            changes.append(f"slot {slot} enabled")
    return changes


# --- storage -------------------------------------------------------------------
def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "profile"


def profile_dir(model_key: str) -> Path:
    d = config_dir() / "profiles" / model_key.replace(":", "-")
    d.mkdir(parents=True, exist_ok=True)
    return d


def profile_path(model_key: str, name: str) -> Path:
    return profile_dir(model_key) / f"{_slug(name)}.json"


def list_profiles(model_key: str) -> list[Profile]:
    out = []
    for f in sorted(profile_dir(model_key).glob("*.json")):
        try:
            out.append(Profile.from_dict(json.loads(f.read_text())))
        except (ValueError, KeyError) as e:
            log.warning("skipping broken profile %s: %s", f, e)
    return out


def load_profile(model_key: str, name: str) -> Profile:
    return Profile.from_dict(json.loads(profile_path(model_key, name).read_text()))


def save_profile(profile: Profile) -> Path:
    path = profile_path(profile.device, profile.name)
    path.write_text(json.dumps(profile.to_dict(), indent=2))
    return path


def delete_profile(model_key: str, name: str) -> None:
    profile_path(model_key, name).unlink(missing_ok=True)


# Which host profile the daemon should apply to each device (absent = leave device in on-board mode).
def _active_path() -> Path:
    return config_dir() / "active.json"


def get_active() -> dict[str, str]:
    try:
        return json.loads(_active_path().read_text())
    except (OSError, ValueError):
        return {}


def set_active(model_key: str, name: str | None) -> None:
    active = get_active()
    if name is None:
        active.pop(model_key, None)
    else:
        active[model_key] = name
    _active_path().write_text(json.dumps(active, indent=2))
