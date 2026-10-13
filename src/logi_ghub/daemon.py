"""logi-ghubd: runs host-mode profiles (what G HUB does while it is open).

For each device that has an active computer profile (``logi-ghub-cli profile
activate NAME``) the daemon:

* switches the device out of on-board mode and applies DPI and report rate,
* for mice with Mouse Button Spy (0x8110): makes buttons whose action is not a
  plain mouse button silent, watches button presses and performs the action
  (keys, media keys, DPI changes, DPI shift, G-Shift layer, macros),
* claims lighting and applies the profile's effects and per-key colours,
* re-applies everything when the device reconnects after sleep, and hands the
  device back to its on-board profile when it exits.

Devices without an active profile are left alone.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import threading
import time

from . import keys, profiles
from .actions import Action
from .device import LogiDevice, find_devices
from .features import lighting
from .features import onboard as ob
from .features.inputs import bitmask_changes
from .hidpp import DeviceOffline, HIDPPError, Message
from .hidpp.protocol import DAEMON_SW_ID
from .hidpp import features as F
from .macros import Macro
from .uinput import VirtualInput

log = logging.getLogger("logi-ghubd")


class MacroRunner:
    """Plays macros on the virtual input device; one thread per running macro."""

    def __init__(self, vinput: VirtualInput | None):
        self.vinput = vinput
        self._running: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _play_once(self, macro: Macro, stop: threading.Event) -> None:
        held: list[int] = []
        try:
            for ev, arg in macro.events():
                if stop.is_set() and macro.repeat != "once":
                    break
                if ev == "delay":
                    stop.wait(int(arg) / 1000)
                elif ev in ("key_down", "key_up"):
                    code = keys.HID_TO_EVDEV[keys.hid_for(str(arg))]
                    self._key(code, ev == "key_down", held)
                elif ev in ("mouse_down", "mouse_up"):
                    code = keys.MOUSE_BUTTONS[int(arg)][1]
                    self._key(code, ev == "mouse_down", held)
                elif ev == "consumer":
                    code = keys.CONSUMER_BY_NAME[str(arg)][1]
                    self._key(code, True, held)
                    self._key(code, False, held)
        finally:
            for code in held:  # never leave a key stuck down
                self._key(code, False, [])

    def _key(self, code: int, down: bool, held: list[int]) -> None:
        if self.vinput is None:
            return
        self.vinput.key(code, down)
        if down:
            held.append(code)
        elif code in held:
            held.remove(code)

    def press(self, key: str, macro: Macro) -> None:
        with self._lock:
            running = self._running.get(key)
            if macro.repeat == "toggle" and running is not None:
                running.set()
                return
            if running is not None and not running.is_set():
                return  # already playing
            stop = threading.Event()
            self._running[key] = stop

        def run():
            try:
                while True:
                    self._play_once(macro, stop)
                    if macro.repeat == "once" or stop.is_set():
                        break
            finally:
                with self._lock:
                    if self._running.get(key) is stop:
                        del self._running[key]

        threading.Thread(target=run, name=f"macro-{macro.name}", daemon=True).start()

    def release(self, key: str, macro: Macro) -> None:
        if macro.repeat == "while_held":
            with self._lock:
                stop = self._running.get(key)
            if stop:
                stop.set()

    def stop_all(self) -> None:
        with self._lock:
            for stop in self._running.values():
                stop.set()


class Session:
    """A device running a host profile."""

    def __init__(self, dev: LogiDevice, profile: profiles.Profile, events: queue.Queue, runner: MacroRunner,
                 vinput: VirtualInput | None):
        self.dev = dev
        self.profile = profile
        self.events = events
        self.runner = runner
        self.vinput = vinput
        self.buttons_down = 0
        self.gshift = False
        self.dpi_index = profile.default_dpi
        self.held_codes: dict[int, list[int]] = {}
        self.pressed: dict[int, Action] = {}  # action used at press time, so release matches it
        self.spy = dev.button_spy
        self.profile_mtime = 0.0
        self.lighting_claimed = False
        self.lights_off = False
        self.slots = dev.binding_count() or (self.spy.count if self.spy else 0)
        self.rom: ob.OnboardProfile | None = None
        if dev.onboard:
            try:
                self.rom = dev.onboard.read_profile(ob.ROM_BASE + 1)
            except (HIDPPError, TimeoutError):
                self.rom = None

    # -- setup / teardown ------------------------------------------------------
    def apply(self) -> None:
        dev, p = self.dev, self.profile
        if dev.onboard:
            dev.onboard.set_mode(ob.MODE_HOST)
        if dev.dpi and p.dpi_stages:
            self.dpi_index = min(p.default_dpi, len(p.dpi_stages) - 1)
            dev.dpi.set(p.dpi_stages[self.dpi_index])
        if dev.report_rate and p.report_rate:
            try:
                dev.report_rate.set_hz(p.report_rate)
            except (HIDPPError, KeyError) as e:
                log.warning("%s: report rate %s not applied: %s", dev.name, p.report_rate, e)
        if self.spy:
            self.spy.set_remapping(self._remap_table())
            self.spy.start()
        self._apply_lighting()
        log.info("%s: profile '%s' applied in host mode", dev.name, p.name)

    def _apply_lighting(self) -> None:
        light = self.profile.lighting or {}
        rgb = self.dev.rgb
        if rgb and (light.get("zones") or light.get("keys")):
            if not self.lighting_claimed:
                rgb.claim()
                self.lighting_claimed = True
            for z, eff in (light.get("zones") or {}).items():
                try:
                    rgb.set_effect(int(z), lighting.Effect.from_dict(eff))
                except (ValueError, HIDPPError) as e:
                    log.warning("%s: zone %s effect not applied: %s", self.dev.name, z, e)
            if light.get("keys") and self.dev.per_key:
                self.dev.per_key.set_colors({int(k): int(v) for k, v in light["keys"].items()})
        if self.dev.brightness and light.get("brightness") is not None:
            self.dev.brightness.set(int(light["brightness"]))

    def release(self) -> None:
        dev = self.dev
        try:
            if self.spy:
                self.spy.stop()
                self.spy.set_remapping(self.spy.identity())
            if self.lighting_claimed:
                dev.rgb.release()
                self.lighting_claimed = False
            elif dev.onboard:
                dev.onboard.set_mode(ob.MODE_ONBOARD)
        except (HIDPPError, DeviceOffline, TimeoutError, OSError) as e:
            log.info("%s: could not hand back cleanly (%s)", dev.name, e)
        self.release_all_keys()

    def on_activity(self, active: bool) -> None:
        """Switch LEDs off when the firmware reports the user idle, back on when they return."""
        rgb = self.dev.rgb
        if not active and not self.lights_off:
            rgb.idle_watch()
            rgb.power(False)
            self.lights_off = True
            log.info("%s: idle, lighting off", self.dev.name)
        elif active and self.lights_off:
            rgb.power(True)
            rgb.claim()
            self.lights_off = False
            self._apply_lighting()
            log.info("%s: active, lighting restored", self.dev.name)

    def release_all_keys(self) -> None:
        for codes in self.held_codes.values():
            for code in codes:
                if self.vinput:
                    self.vinput.key(code, False)
        self.held_codes.clear()

    # -- button handling --------------------------------------------------------
    def action_for(self, index: int) -> Action:
        layer = self.profile.gshift_buttons if self.gshift else self.profile.buttons
        a = layer.get(index)
        if a is None and self.gshift:
            a = self.profile.buttons.get(index)
        if (a is None or a.kind == "default") and self.rom is not None:
            a = self.rom.button(index)
        return a or Action("default")

    def _remap_table(self) -> list[int]:
        """Physical buttons whose action is a plain mouse button keep sending it natively."""
        table = []
        for i in range(self.spy.count):
            a = self.action_for(i)
            table.append(a.button if a.kind == "mouse" and not self._gshift_differs(i) else 0)
        return table

    def _gshift_differs(self, i: int) -> bool:
        g = self.profile.gshift_buttons.get(i)
        return g is not None and g.kind not in ("default",) and g != self.profile.buttons.get(i)

    def on_message(self, msg: Message) -> None:
        if self.spy:
            mask = self.spy.parse(msg)
            if mask is not None:
                pressed, released = bitmask_changes(self.buttons_down, mask, self.spy.count)
                self.buttons_down = mask
                for i in pressed:
                    self.press(i)
                for i in released:
                    self.release_button(i)
                return
        if self.lighting_claimed and isinstance(self.dev.rgb, lighting.RGBEffects):
            active = self.dev.rgb.activity(msg)
            if active is not None:
                self.events.put(("activity", self, active))
                return
        if self.dev.has(F.WIRELESS_DEVICE_STATUS) and msg.sub_id == self.dev.hidpp.features[F.WIRELESS_DEVICE_STATUS] and msg.swid == 0:
            log.info("%s: reconnected, re-applying profile", self.dev.name)
            self.events.put(("reapply", self))

    def press(self, i: int) -> None:
        a = self.action_for(i)
        self.pressed[i] = a
        k = a.kind
        codes: list[int] = []
        if k == "mouse":
            if self._remap_table()[i] == 0:
                codes = [keys.MOUSE_BUTTONS.get(a.button, ("", 0x110))[1]]
        elif k == "key":
            codes = [keys.HID_TO_EVDEV[keys.hid_for(keys.MODIFIERS[b])] for b in range(8) if a.modifiers & (1 << b)]
            if a.key in keys.HID_TO_EVDEV:
                codes.append(keys.HID_TO_EVDEV[a.key])
        elif k == "consumer":
            ev = keys.CONSUMER_TO_EVDEV.get(a.usage)
            codes = [ev] if ev else []
        elif k == "special":
            self._special(a, True)
        elif k == "macro":
            macro = self.profile.macro(a.macro)
            if macro:
                self.runner.press(f"{self.dev.key}:{i}", macro)
        if codes and self.vinput:
            for c in codes:
                self.vinput.key(c, True)
            self.held_codes[i] = codes

    def release_button(self, i: int) -> None:
        codes = self.held_codes.pop(i, [])
        for c in reversed(codes):
            if self.vinput:
                self.vinput.key(c, False)
        a = self.pressed.pop(i, None) or self.action_for(i)
        if a.kind == "special":
            self._special(a, False)
        elif a.kind == "macro":
            macro = self.profile.macro(a.macro)
            if macro:
                self.runner.release(f"{self.dev.key}:{i}", macro)

    def _special(self, a: Action, down: bool) -> None:
        f = a.function
        stages = self.profile.dpi_stages
        dpi = self.dev.dpi
        if f == 0x0B:  # G-Shift while held
            self.gshift = down
            return
        if f == 0x07 and dpi:  # DPI shift while held
            dpi.set(self.profile.shift_dpi if down else stages[self.dpi_index])
            return
        if not down:
            return
        if f in (0x03, 0x04, 0x05, 0x06) and dpi and stages:
            if f == 0x03:
                self.dpi_index = min(self.dpi_index + 1, len(stages) - 1)
            elif f == 0x04:
                self.dpi_index = max(self.dpi_index - 1, 0)
            elif f == 0x05:
                self.dpi_index = (self.dpi_index + 1) % len(stages)
            else:
                self.dpi_index = self.profile.default_dpi
            dpi.set(stages[self.dpi_index])
            log.info("%s: DPI %d", self.dev.name, stages[self.dpi_index])
        elif f in (0x01, 0x02, 0x10, 0x11) and self.vinput:
            horizontal = f in (0x01, 0x02)
            self.vinput.wheel(-1 if f in (0x01, 0x10) else 1, horizontal)


def _profile_mtime(model_key: str, name: str) -> float:
    try:
        return os.stat(profiles.profile_dir(model_key) / f"{profiles._slug(name)}.json").st_mtime
    except OSError:
        return 0.0


class Daemon:
    def __init__(self, fake: bool = False):
        self.fake = fake
        self.events: queue.Queue = queue.Queue()
        self.sessions: dict[str, Session] = {}
        self.stop = threading.Event()
        try:
            self.vinput = VirtualInput()
        except OSError as e:
            log.warning("no virtual input device (%s); key actions and macros will not work", e)
            self.vinput = None
        self.runner = MacroRunner(self.vinput)
        self._active_mtime = 0.0
        self._last_scan = 0.0

    def _devices(self) -> list[LogiDevice]:
        if self.fake:
            from .hidpp.fake import fake_devices

            return fake_devices()
        return find_devices()

    def scan(self) -> None:
        active = profiles.get_active()
        # Drop sessions whose profile was deactivated or changed.
        for key, s in list(self.sessions.items()):
            if active.get(key) != s.profile.name or s.profile_mtime != _profile_mtime(key, s.profile.name):
                self.end(key)
        wanted = {k: v for k, v in active.items() if k not in self.sessions}
        if not wanted:
            return
        for dev in self._devices():
            name = wanted.get(dev.key)
            if not name or not dev.online:
                dev.close()
                continue
            try:
                profile = profiles.load_profile(dev.key, name)
                dev.hidpp.swid = DAEMON_SW_ID
                session = Session(dev, profile, self.events, self.runner, self.vinput)
                session.profile_mtime = _profile_mtime(dev.key, name)
                dev.hidpp.add_handler(lambda m, s=session: self.events.put(("msg", s, m)))
                dev.hidpp.start_listener()
                session.apply()
                self.sessions[dev.key] = session
            except (OSError, HIDPPError, DeviceOffline, TimeoutError, ValueError) as e:
                log.warning("%s: cannot start profile '%s': %s", dev.name, name, e)
                dev.close()

    def end(self, key: str) -> None:
        s = self.sessions.pop(key, None)
        if s:
            s.release()
            s.dev.close()
            log.info("%s: released", s.dev.name)

    def run(self) -> None:
        log.info("logi-ghubd started")
        while not self.stop.is_set():
            now = time.monotonic()
            try:
                mtime = os.stat(profiles._active_path()).st_mtime
            except OSError:
                mtime = 0.0
            if mtime != self._active_mtime or now - self._last_scan > 10:
                self._active_mtime = mtime
                self._last_scan = now
                self.scan()
            try:
                item = self.events.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                if item[0] == "msg":
                    item[1].on_message(item[2])
                elif item[0] == "activity":
                    item[1].on_activity(item[2])
                elif item[0] == "reapply":
                    item[1].apply()
                elif item[0] == "reload":
                    for key in list(self.sessions):
                        self.end(key)
                    self.scan()
            except (HIDPPError, DeviceOffline, TimeoutError) as e:
                log.warning("device request failed: %s", e)
            except OSError as e:
                s = item[1]
                log.warning("%s: lost (%s)", s.dev.name, e)
                self.sessions.pop(s.dev.key, None)
        self.shutdown()

    def shutdown(self) -> None:
        self.runner.stop_all()
        for key in list(self.sessions):
            self.end(key)
        if self.vinput:
            self.vinput.close()
        log.info("logi-ghubd stopped")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="logi-ghubd", description="Run host-mode Logitech profiles")
    ap.add_argument("--fake", action="store_true", help="use simulated devices")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.fake:
        from .config import use_sandbox

        use_sandbox()
    d = Daemon(fake=args.fake)
    signal.signal(signal.SIGTERM, lambda *_: d.stop.set())
    signal.signal(signal.SIGINT, lambda *_: d.stop.set())
    signal.signal(signal.SIGHUP, lambda *_: d.events.put(("reload",)))
    d.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
