"""High-level device wrapper: opens a HID++ device and exposes the features it has."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cached_property

from .features import basic, lighting, onboard
from .features.inputs import GKeys, MouseButtonSpy
from .hidpp import Device, DeviceOffline, FeatureNotSupported, HIDPPError, HidrawInfo, HidrawTransport, enumerate_hidpp
from .hidpp import features as F

log = logging.getLogger(__name__)


@dataclass
class Model:
    name: str
    kind: str
    # Names of on-board binding slots, by index. Slots past the list are shown as "Button n".
    buttons: list[str] = field(default_factory=list)


_G502X_BUTTONS = [
    "Left click", "Right click", "Middle click (wheel)", "Back (G4)", "Sniper / DPI shift (G6)", "Forward (G5)",
    "Wheel tilt left", "Wheel tilt right", "Profile button (G9)", "DPI up (G8)", "DPI down (G7)",
    "Wheel down", "Wheel up",
]

# Button names come from the factory (ROM) profile of each device.
MODELS: dict[str, Model] = {
    "046d:409f": Model("G502 X LIGHTSPEED", "mouse", _G502X_BUTTONS),
    "046d:c098": Model("G502 X LIGHTSPEED (USB)", "mouse", _G502X_BUTTONS),
    "046d:c099": Model("G502 X", "mouse", _G502X_BUTTONS),
    "046d:4099": Model("G502 X PLUS", "mouse", _G502X_BUTTONS),
    "046d:c095": Model("G502 X PLUS (USB)", "mouse", _G502X_BUTTONS),
    "046d:40b6": Model("G915 X LIGHTSPEED TKL", "keyboard", []),
}


class LogiDevice:
    def __init__(self, info: HidrawInfo):
        self.info = info
        self.hidpp: Device | None = None
        self.name = info.name
        self.kind = MODELS.get(info.key, Model(info.name, "device")).kind
        self.online = False
        self.error: str | None = None
        self.unit_id: int | None = None
        self.model_ids: list[int] = []

    @property
    def key(self) -> str:
        """Model id used for profiles: the wireless product id, also when the device is plugged in by USB."""
        if self.model_ids:
            return f"{self.info.vid:04x}:{self.model_ids[0]:04x}"
        return self.info.key

    @property
    def model(self) -> Model:
        return MODELS.get(self.key, Model(self.name, self.kind))

    def open(self) -> "LogiDevice":
        transport = HidrawTransport(self.info.path)
        self.hidpp = Device(transport, short_ok=self.info.supports_short)
        try:
            self.hidpp.ping()
            self.hidpp.discover_features()
            self.name = basic.device_name(self.hidpp)
            self.kind = basic.device_kind(self.hidpp)
            try:
                self.unit_id, self.model_ids = basic.model_ids(self.hidpp)
            except (HIDPPError, FeatureNotSupported):
                pass
            self.online = True
        except DeviceOffline as e:
            self.error = str(e)
            self.online = False
        except (TimeoutError, HIDPPError, OSError) as e:
            self.error = str(e)
            self.online = False
        return self

    def close(self) -> None:
        if self.hidpp:
            self.hidpp.close()
            self.hidpp = None

    def has(self, feature_id: int) -> bool:
        return bool(self.hidpp) and feature_id in self.hidpp.features

    def _maybe(self, feature_id: int, factory):
        if not self.has(feature_id):
            return None
        try:
            return factory(self.hidpp)
        except (FeatureNotSupported, HIDPPError, LookupError) as e:
            log.info("%s: %s unavailable: %s", self.name, F.name(feature_id), e)
            return None

    @cached_property
    def dpi(self) -> basic.AdjustableDpi | None:
        return self._maybe(F.ADJUSTABLE_DPI, basic.AdjustableDpi)

    @cached_property
    def report_rate(self) -> basic.ReportRate | None:
        if self.has(F.EXTENDED_ADJUSTABLE_REPORT_RATE) or self.has(F.REPORT_RATE):
            return basic.ReportRate(self.hidpp)
        return None

    @cached_property
    def onboard(self) -> onboard.OnboardProfiles | None:
        ob = self._maybe(F.ONBOARD_PROFILES, onboard.OnboardProfiles)
        if ob is not None and not ob.info.supported:
            log.warning("%s: on-board memory format %s not supported", self.name, ob.info)
            return None
        return ob

    @cached_property
    def rgb(self) -> lighting.RGBEffects | lighting.ColorLedEffects | None:
        return self._maybe(F.RGB_EFFECTS, lighting.RGBEffects) or self._maybe(F.COLOR_LED_EFFECTS, lighting.ColorLedEffects)

    @cached_property
    def per_key(self) -> lighting.PerKeyLighting | None:
        return self._maybe(F.PER_KEY_LIGHTING_V2, lighting.PerKeyLighting)

    @cached_property
    def brightness(self) -> lighting.Brightness | None:
        return self._maybe(F.BRIGHTNESS_CONTROL, lighting.Brightness)

    @cached_property
    def button_spy(self) -> MouseButtonSpy | None:
        return self._maybe(F.MOUSE_BUTTON_SPY, MouseButtonSpy)

    @cached_property
    def gkeys(self) -> GKeys | None:
        return self._maybe(F.GKEY, GKeys)

    def battery(self) -> basic.Battery | None:
        try:
            return basic.battery(self.hidpp)
        except (HIDPPError, FeatureNotSupported, TimeoutError):
            return None

    def firmware(self) -> list[basic.Firmware]:
        try:
            return basic.firmware(self.hidpp)
        except (HIDPPError, FeatureNotSupported, TimeoutError):
            return []

    def button_name(self, index: int) -> str:
        names = self.model.buttons
        return names[index] if index < len(names) else f"Button {index + 1}"

    def binding_count(self) -> int:
        """How many binding slots to show: physical buttons, plus wheel slots the factory profile fills."""
        if self.onboard is None:
            return 0
        return max(self.onboard.info.button_count, len(self.model.buttons))

    def describe(self) -> dict:
        out = {
            "name": self.name,
            "model": self.key,
            "path": self.info.path,
            "kind": self.kind,
            "online": self.online,
        }
        if not self.online:
            out["error"] = self.error
            return out
        out["protocol"] = "%d.%d" % self.hidpp.protocol
        out["features"] = {F.short_name(fid): idx for fid, idx in sorted(self.hidpp.features.items(), key=lambda x: x[1])}
        out["firmware"] = [f"{f.kind} {f.name} {f.version}".strip() for f in self.firmware()]
        b = self.battery()
        if b:
            out["battery"] = {"percent": b.percent, "level": b.level, "charging": b.charging}
        return out


def find_devices(open_them: bool = True) -> list[LogiDevice]:
    from .features.g600 import G600Device, enumerate_g600

    devs = [LogiDevice(i) for i in enumerate_hidpp()] + [G600Device(i) for i in enumerate_g600()]
    if open_them:
        for d in devs:
            try:
                d.open()
            except OSError as e:
                d.error = f"cannot open {d.info.path}: {e}"
        # A device plugged in by USB is also still listed (asleep) behind its receiver: show it once.
        reachable = {pid for d in devs if d.online for pid in d.model_ids}
        hidden = [d for d in devs if not d.online and d.info.pid in reachable]
        for d in hidden:
            d.close()
        devs = [d for d in devs if d not in hidden]
    return devs


def select_device(devs: list[LogiDevice], selector: str | None) -> LogiDevice:
    """Pick a device by model key, hidraw path, kind or a substring of its name."""
    if not devs:
        raise LookupError("no Logitech HID++ devices found (is the udev rule installed?)")
    if selector is None:
        online = [d for d in devs if d.online]
        if len(online) == 1:
            return online[0]
        raise LookupError("several devices found; pick one with --device (" + ", ".join(f"{d.key} {d.name}" for d in devs) + ")")
    s = selector.lower()
    for d in devs:
        if s in (d.key, d.info.key, d.info.path, d.kind) or s in d.name.lower():
            return d
    raise LookupError(f"no device matches {selector!r}")
