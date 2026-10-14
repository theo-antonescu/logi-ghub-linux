"""logi-ghubd: runs host-mode profiles (what G HUB does while it is open).

For each device that has an active computer profile (``logi-ghub-cli profile
activate NAME``) the daemon:

* switches the device out of on-board mode and applies DPI and report rate,
* for mice with Mouse Button Spy (0x8110): makes buttons whose action is not a
  plain mouse button silent, watches button presses and performs the action
  (keys, media keys, DPI changes, DPI shift, G-Shift layer, macros),
* claims lighting and applies the profile's effects and per-key colours,
* re-applies everything when the device reconnects after sleep, and hands the
  device back to its on-board profile when it exits,
* runs the profile's Lua script, if it has one (see :mod:`logi_ghub.scripting`):
  ``OnEvent`` gets PROFILE_ACTIVATED/DEACTIVATED and every mouse button press
  and release, and the script's PressKey, MoveMouseRelative, SetBacklightColor
  and so on act through the virtual input device and the session's devices.

Devices without an active profile are left alone.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import queue
import signal
import threading
import time

from . import inputstate, keys, lightfx, profiles, screens, scripting
from .actions import Action
from .device import LogiDevice, find_devices
from .features import lighting
from .features import onboard as ob
from .features.inputs import bitmask_changes
from .hidpp import DeviceOffline, HIDPPError, Message
from .hidpp.protocol import DAEMON_SW_ID
from .hidpp import features as F
from .macros import Macro
from .uinput import VirtualInput, VirtualPointer

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
                if stop.is_set() and (macro.repeat != "once" or getattr(stop, "hard", False)):
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
            held: list[int] = []
            try:
                for name in macro.hold:  # e.g. Alt stays down while Tab repeats
                    self._key(keys.HID_TO_EVDEV[keys.hid_for(name)], True, held)
                while True:
                    self._play_once(macro, stop)
                    if macro.repeat == "once" or stop.is_set():
                        break
                    if macro.interval_ms and stop.wait(macro.interval_ms / 1000):
                        break
            finally:
                for code in reversed(held):
                    self._key(code, False, [])
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

    def running(self, key: str) -> bool:
        with self._lock:
            stop = self._running.get(key)
            return stop is not None and not stop.is_set()

    def abort(self, key: str) -> None:
        """Stop a macro mid-way, even one that plays once (scripts' AbortMacro)."""
        with self._lock:
            stop = self._running.get(key)
        if stop:
            stop.hard = True
            stop.set()


_input_state: inputstate.InputState | None = None


def input_state() -> inputstate.InputState:
    global _input_state
    if _input_state is None:
        _input_state = inputstate.InputState()
    return _input_state


class SessionScriptHost(scripting.ScriptHost):
    """The script API for a host-mode session: real input, the running sessions' devices, the script log."""

    def __init__(self, session: "Session"):
        super().__init__("mouse" if session.dev.kind == "mouse" else "kb")
        self.session = session
        self.log_file = scripting.log_path(session.dev.key, session.profile.name)
        self.pointer: VirtualPointer | None = None
        self.macro_key = f"script:{session.dev.key}"
        self._warned: set[str] = set()

    @property
    def vinput(self) -> VirtualInput | None:
        return self.session.vinput

    def _warn_once(self, key: str, text: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.log(text + "\n")

    # -- output --------------------------------------------------------------------------------
    def record(self, text: str) -> None:
        log.debug("%s script: %s", self.session.dev.name, text)

    def log(self, text: str) -> None:
        scripting.append_log(self.log_file, text)

    def debug(self, text: str) -> None:
        log.info("%s script: %s", self.session.dev.name, text.rstrip())

    def clear_log(self) -> None:
        try:
            self.log_file.write_text("")
        except OSError:
            pass

    def error(self, text: str) -> None:
        super().error(text)
        log.info("%s script error: %s", self.session.dev.name, text)

    def out_key(self, code: int, down: bool, name) -> None:
        if self.vinput:
            self.vinput.key(code, down)

    def out_mouse_button(self, button: int, down: bool) -> None:
        if self.vinput:
            self.vinput.key(scripting.LUA_BUTTON_TO_EVDEV[button], down)

    def out_move_relative(self, dx: int, dy: int) -> None:
        if self.vinput:
            self.vinput.move(dx, dy)

    def out_wheel(self, clicks: int) -> None:
        if self.vinput:
            self.vinput.wheel(clicks)

    def out_move_to(self, x: int, y: int, virtual: bool) -> None:
        if self.pointer is None:
            try:
                self.pointer = VirtualPointer()
            except OSError as e:
                self._warn_once("pointer", f"MoveMouseTo: no virtual pointer ({e})")
                return
        if not virtual:  # MoveMouseTo covers the primary monitor, MoveMouseToVirtual the whole desktop
            x, y = screens.to_desktop(x, y, screens.primary_fraction())
        self.pointer.move_to(x, y)

    def lcd(self, text: str | None, timeout) -> None:
        if text is not None:
            self.log(f"[LCD] {text}\n")

    # -- state ---------------------------------------------------------------------------------
    def modifier_pressed(self, name: str) -> bool:
        return super().modifier_pressed(name) or input_state().pressed(scripting.MODIFIER_CODES.get(name.lower(), ()))

    def key_lock_on(self, name: str) -> bool:
        return inputstate.lock_on(name) if name.lower() in scripting.LOCK_KEYS else False

    def mouse_button_pressed(self, button: int) -> bool:
        if super().mouse_button_pressed(button):
            return True
        try:
            if any(button in s.lua_buttons_down() for s in self.session.peers()):
                return True
        except RuntimeError:  # a session started or ended while we looked
            pass
        code = scripting.LUA_BUTTON_TO_EVDEV.get(button)
        return bool(code) and input_state().pressed((code,))

    # -- macros, DPI, lighting -----------------------------------------------------------------
    def play_macro(self, name: str) -> None:
        macro = self.session.profile.macro(name)
        if macro is None:
            raise ValueError(f"PlayMacro: this profile has no macro named {name!r}")
        if self.session.runner.running(self.macro_key):
            return  # one script macro at a time
        self.session.runner.press(self.macro_key, dataclasses.replace(macro, repeat="once"))

    def abort_macro(self) -> None:
        self.session.runner.abort(self.macro_key)

    def _mouse_session(self) -> "Session | None":
        s = self.session.peer("mouse")
        if s is None or not s.dev.dpi:
            self._warn_once("dpi", "SetMouseDPITable: no mouse with adjustable DPI is running a host-mode profile")
            return None
        return s

    def current_dpi_table(self) -> list[int]:
        s = self.session.peer("mouse")
        return list(s.stages()) if s else []

    def set_dpi(self, table: list[int], index: int) -> None:
        s = self._mouse_session()
        if s:
            s.on_main_thread(lambda: s.set_dpi_table(table, index))

    def set_backlight(self, rgb: int, family: str) -> None:
        kind = "mouse" if family == "mouse" else "keyboard"
        s = self.session.peer(kind)
        if s is None or s.dev.rgb is None:
            self._warn_once(f"backlight-{kind}", f"SetBacklightColor: no {kind} with RGB lighting is running a "
                                                 "host-mode profile, so there is nothing to colour")
            return
        s.on_main_thread(lambda: s.set_backlight(rgb))

    def release_all(self) -> None:
        super().release_all()
        self.session.runner.abort(self.macro_key)

    def close(self) -> None:
        if self.pointer:
            self.pointer.close()
            self.pointer = None


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
        self.gkeys_down = 0
        self.gshift = False
        self.dpi_index = profile.default_dpi
        self.held_codes: dict[int, list[int]] = {}
        self.pressed: dict[int, Action] = {}  # action used at press time, so release matches it
        self.spy = dev.button_spy
        self.profile_mtime = 0.0
        self.lighting_claimed = False
        self.lights_off = False
        self.script_color: int | None = None  # last SetBacklightColor, re-applied after the keyboard sleeps
        self.animator: lightfx.Animator | None = None
        self.slots = dev.binding_count() or (self.spy.count if self.spy else 0)
        self.dpi_override: list[int] | None = None  # set by a script's SetMouseDPITable
        self.peers = lambda: [self]  # every running session; the daemon replaces this
        self.script: scripting.ScriptRunner | None = None
        self.script_host: SessionScriptHost | None = None
        count = self.spy.count if self.spy else 0
        self.script_numbers = scripting.script_button_numbers([dev.button_name(i) for i in range(count)], count)
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
        self._start_script()
        log.info("%s: profile '%s' applied in host mode", dev.name, p.name)

    def _start_script(self) -> None:
        p = self.profile
        if self.script is not None:
            if self.dev.gkeys:  # reconnected: the keyboard forgot software control
                self.dev.gkeys.enable_software_control(True)
            return
        if not p.script_enabled or not p.script.strip():
            return
        host = SessionScriptHost(self)
        if not scripting.available():
            host.error(scripting.MISSING)
            log.warning("%s: %s", self.dev.name, scripting.MISSING)
            return
        self.script_host = host
        self.script = scripting.ScriptRunner(p.script, host, f"{self.dev.name}/{p.name}").start()
        self.script.post("PROFILE_ACTIVATED")
        if self.dev.gkeys:
            self.dev.gkeys.enable_software_control(True)
        log.info("%s: script running", self.dev.name)

    def stop_script(self) -> None:
        if self.script is None:
            return
        self.script.stop()
        self.script = None
        if self.script_host:
            self.script_host.close()
        if self.dev.gkeys:
            try:
                self.dev.gkeys.enable_software_control(False)
            except (HIDPPError, DeviceOffline, TimeoutError, OSError):
                pass

    def _apply_lighting(self) -> None:
        light = self.profile.lighting or {}
        if self.animator:
            self.animator.stop()
            self.animator = None
        if self.dev.rgb and (light.get("zones") or light.get("keys") or (light.get("animation") or {}).get("enabled")):
            self.animator = lightfx.apply(self.dev, light, claim=not self.lighting_claimed)
            self.lighting_claimed = True
        elif self.dev.brightness and light.get("brightness") is not None:
            self.dev.brightness.set(int(light["brightness"]))

    def release(self) -> None:
        dev = self.dev
        self.stop_script()
        try:
            if self.spy:
                self.spy.stop()
                self.spy.set_remapping(self.spy.identity())
            if self.animator:
                self.animator.stop()
                self.animator = None
            if self.lighting_claimed:
                if self.lights_off:  # idle: switch the LEDs back on, or they stay dark after the hand-back
                    dev.rgb.power(True)
                    self.lights_off = False
                dev.rgb.release()
                lightfx.clear_applied(dev.key)
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
            if self.animator:
                self.animator.paused.set()
            rgb.idle_watch()
            rgb.power(False)
            self.lights_off = True
            log.info("%s: idle, lighting off", self.dev.name)
        elif active and self.lights_off:
            rgb.power(True)
            rgb.claim()
            self.lights_off = False
            if self.script_color is not None:  # a script coloured the keyboard: keep its colour
                self.set_backlight(self.script_color)
            else:
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
        if self.script and self.dev.gkeys:
            mask = self.dev.gkeys.parse(msg)
            if mask is not None:
                pressed, released = bitmask_changes(self.gkeys_down, mask)
                self.gkeys_down = mask
                for i in pressed:
                    self.script.post("G_PRESSED", i + 1, "kb")
                for i in released:
                    self.script.post("G_RELEASED", i + 1, "kb")
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
        self._script_button("MOUSE_BUTTON_PRESSED", i)

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
        self._script_button("MOUSE_BUTTON_RELEASED", i)

    # -- script support ------------------------------------------------------------------------
    def _script_button(self, event: str, i: int) -> None:
        if self.script is None:
            return
        n = self.script_numbers.get(i, i + 1)
        if n == 1 and not self.script_host.primary_events:
            return  # like G HUB: the left button is reported only after EnablePrimaryMouseButtonEvents
        self.script.post(event, n, "mouse")

    def lua_buttons_down(self) -> set[int]:
        """Mouse buttons held on this device, numbered like IsMouseButtonPressed (1 left, 2 middle, 3 right, 4, 5)."""
        out = set()
        for i in range(self.spy.count if self.spy else 0):
            if self.buttons_down & (1 << i):
                a = self.pressed.get(i) or self.action_for(i)
                if a.kind == "mouse" and a.button in scripting.HID_BUTTON_TO_LUA:
                    out.add(scripting.HID_BUTTON_TO_LUA[a.button])
        return out

    def peer(self, kind: str) -> "Session | None":
        try:
            return next((s for s in self.peers() if s.dev.kind == kind), None)
        except RuntimeError:  # sessions changed while we looked
            return None

    def on_main_thread(self, fn) -> None:
        """Run ``fn`` on the daemon's thread, which owns the device state (scripts run on their own)."""
        if self.events is None:
            fn()
        else:
            self.events.put(("call", self, fn))

    def stages(self) -> list[int]:
        return self.dpi_override or self.profile.dpi_stages

    def set_dpi_table(self, table: list[int], index: int) -> None:
        if not self.dev.dpi:
            return
        self.dpi_override = list(table)
        self.dpi_index = index
        self.dev.dpi.set(table[index])
        log.info("%s: script set DPI %d", self.dev.name, table[index])

    def set_backlight(self, color: int) -> None:
        """Whole-keyboard colour from a script. Not saved to the keyboard; the profile's lighting returns on reapply."""
        rgb, pk = self.dev.rgb, self.dev.per_key
        if rgb is None:
            return
        self.script_color = color
        if self.lights_off:
            return  # applied when the keyboard wakes up
        if self.animator:
            self.animator.stop()
            self.animator = None
        if not self.lighting_claimed:
            rgb.claim()
            self.lighting_claimed = True
        for z in range(len(rgb.zones)):
            if rgb.zones[z].effect_by_id(0x01):
                rgb.set_effect(z, lighting.Effect(0x01, color=color), persist=False)
        if pk:
            pk.set_colors({z: color for z in pk.zones})

    def _special(self, a: Action, down: bool) -> None:
        f = a.function
        stages = self.stages()
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
                self.dpi_index = min(self.profile.default_dpi, len(stages) - 1)
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
            if dev.hidpp is None:
                log.warning("%s: host mode is not supported on this device; it keeps its on-board profiles", dev.name)
                dev.close()
                continue
            try:
                profile = profiles.load_profile(dev.key, name)
                dev.hidpp.swid = DAEMON_SW_ID
                session = Session(dev, profile, self.events, self.runner, self.vinput)
                # itself first: its script may start before it is registered below
                session.peers = lambda s=session: [s] + [x for x in list(self.sessions.values()) if x is not s]
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
                elif item[0] == "call":
                    if self.sessions.get(item[1].dev.key) is item[1]:  # skip calls for a session that ended
                        item[2]()
                elif item[0] == "reload":
                    for key in list(self.sessions):
                        self.end(key)
                    self.scan()
            except (HIDPPError, DeviceOffline, TimeoutError, ValueError) as e:
                log.warning("device request failed: %s", e)
            except OSError as e:
                s = item[1]
                log.warning("%s: lost (%s)", s.dev.name, e)
                s.stop_script()
                self.sessions.pop(s.dev.key, None)
        self.shutdown()

    def shutdown(self) -> None:
        self.runner.stop_all()
        for key in list(self.sessions):
            self.end(key)
        if _input_state:
            _input_state.close()
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
