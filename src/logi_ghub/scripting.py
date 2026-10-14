"""Lua scripts in profiles, with the G-series Lua API that G HUB and LGS offer.

G HUB never puts a script on the device: it runs it on the PC while G HUB is
open, calling the script's ``OnEvent(event, arg, family)`` for each button
press, profile switch and so on. Here the host-mode daemon does the same for
the profile it runs. A script gets the documented API (``PressKey``,
``MoveMouseRelative``, ``Sleep``, ``IsMouseButtonPressed``, ``PlayMacro``,
``SetBacklightColor``, ``SetMouseDPITable`` ...) and the string, table and math
libraries, but no ``io``, ``os``, ``require`` or ``debug``, so a script pasted
from the internet can't touch files or run programs.

Lua comes from the ``lupa`` Python package (``pip install lupa``, or
``python3-lupa`` on Ubuntu). Everything else in the program works without it.

Pieces:

* :class:`LuaScript` compiles a script into its sandbox and calls ``OnEvent``;
  it runs on whichever thread calls it.
* :class:`ScriptHost` is what the API functions act on. The base class only
  records what the script did and keeps the simulated key/button state;
  the daemon subclasses it to type through the virtual input device.
* :class:`ScriptRunner` gives a script its own thread and event queue, like
  G HUB (one ``OnEvent`` at a time, in order; ``Sleep`` doesn't block the service).
* :func:`simulate` runs a script against a list of timed events on a virtual
  clock and returns what it would have done, without sending any input.
"""

from __future__ import annotations

import importlib
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import data_dir

log = logging.getLogger(__name__)

EVENTS = ("PROFILE_ACTIVATED", "PROFILE_DEACTIVATED", "G_PRESSED", "G_RELEASED", "M_PRESSED", "M_RELEASED",
          "MOUSE_BUTTON_PRESSED", "MOUSE_BUTTON_RELEASED")
FAMILIES = ("kb", "lhc", "mouse")

EXAMPLE = """\
-- Runs while this profile is active in host mode (the logi-ghubd service).
-- OnEvent is called for every event; see the Script page for button numbers.

function OnEvent(event, arg, family)
    if event == "PROFILE_ACTIVATED" then
        OutputLogMessage("Profile activated\\n")
    end

    -- Example: button 4 (Back) types Ctrl+C, button 5 (Forward) types Ctrl+V
    if event == "MOUSE_BUTTON_PRESSED" and arg == 4 then
        PressAndReleaseKey("lctrl", "c")
    elseif event == "MOUSE_BUTTON_PRESSED" and arg == 5 then
        PressKey("lctrl")
        PressAndReleaseKey("v")
        ReleaseKey("lctrl")
    end
end
"""

# --- key names and scancodes (Appendix A of the G-series Lua API) ---------------------------
# Scancodes are Windows "set 1" codes; 0x1xx means the key sends an E0 prefix.
KEYNAMES: dict[str, int] = {
    "escape": 0x01, **{f"f{i}": 0x3A + i for i in range(1, 11)}, "f11": 0x57, "f12": 0x58,
    **{f"f{i}": 0x64 + i - 13 for i in range(13, 24)}, "f24": 0x76,
    "printscreen": 0x137, "scrolllock": 0x46, "pause": 0x146, "tilde": 0x29,
    **{str(d): 0x02 + (d - 1) for d in range(1, 10)}, "0": 0x0B, "minus": 0x0C, "equal": 0x0D, "backspace": 0x0E,
    "tab": 0x0F, "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15, "u": 0x16, "i": 0x17,
    "o": 0x18, "p": 0x19, "lbracket": 0x1A, "rbracket": 0x1B, "backslash": 0x2B, "capslock": 0x3A,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23, "j": 0x24, "k": 0x25, "l": 0x26,
    "semicolon": 0x27, "quote": 0x28, "enter": 0x1C, "lshift": 0x2A, "non_us_slash": 0x56, "z": 0x2C,
    "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30, "n": 0x31, "m": 0x32, "comma": 0x33, "period": 0x34,
    "slash": 0x35, "rshift": 0x36, "lctrl": 0x1D, "lgui": 0x15B, "lalt": 0x38, "spacebar": 0x39, "ralt": 0x138,
    "rgui": 0x15C, "appkey": 0x15D, "rctrl": 0x11D, "insert": 0x152, "home": 0x147, "pageup": 0x149,
    "delete": 0x153, "end": 0x14F, "pagedown": 0x151, "up": 0x148, "left": 0x14B, "down": 0x150, "right": 0x14D,
    "numlock": 0x45, "numslash": 0x135, "numminus": 0x4A, "num7": 0x47, "num8": 0x48, "num9": 0x49,
    "numplus": 0x4E, "num4": 0x4B, "num5": 0x4C, "num6": 0x4D, "num1": 0x4F, "num2": 0x50, "num3": 0x51,
    "numenter": 0x11C, "num0": 0x52, "numperiod": 0x53,
}

# Set 1 scancodes 0x01-0x58 are the Linux key codes. The rest:
_SCANCODE_TO_EVDEV = {
    **{0x64 + i: 183 + i for i in range(11)}, 0x76: 194,  # F13-F23, F24
    0x11C: 96, 0x11D: 97, 0x135: 98, 0x137: 99, 0x138: 100, 0x146: 119, 0x147: 102, 0x148: 103, 0x149: 104,
    0x14B: 105, 0x14D: 106, 0x14F: 107, 0x150: 108, 0x151: 109, 0x152: 110, 0x153: 111, 0x15B: 125, 0x15C: 126,
    0x15D: 127,
    # media keys scripts send by scancode (not in the reference, common in shared scripts)
    0x120: 113, 0x12E: 114, 0x130: 115, 0x122: 164, 0x124: 166, 0x110: 165, 0x119: 163,
}

MODIFIER_CODES = {
    "lalt": (56,), "ralt": (100,), "alt": (56, 100),
    "lshift": (42,), "rshift": (54,), "shift": (42, 54),
    "lctrl": (29,), "rctrl": (97,), "ctrl": (29, 97),
    "lgui": (125,), "rgui": (126,), "gui": (125, 126),
}
LOCK_KEYS = ("capslock", "numlock", "scrolllock")

# PressMouseButton & co: 1 left, 2 middle, 3 right, 4 X1 (back), 5 X2 (forward).
LUA_BUTTON_TO_EVDEV = {1: 0x110, 2: 0x112, 3: 0x111, 4: 0x113, 5: 0x114}
# Mouse HID button numbers (1 left, 2 right, 3 middle, 4 back, 5 forward) to the numbering above.
HID_BUTTON_TO_LUA = {1: 1, 2: 3, 3: 2, 4: 4, 5: 5}


def evdev_code(key: str | int | float) -> int:
    """Linux key code for a script key name ("a", "lctrl", "f13") or set-1 scancode (30, 0x1d)."""
    if isinstance(key, str):
        name = key.strip().lower()
        if name not in KEYNAMES:
            raise ValueError(f"unknown key {key!r}")
        sc = KEYNAMES[name]
    else:
        sc = int(key)
    if 0 < sc <= 0x58:
        return sc
    if sc in _SCANCODE_TO_EVDEV:
        return _SCANCODE_TO_EVDEV[sc]
    raise ValueError(f"unsupported scancode {sc:#x}")


def script_button_numbers(names: list[str], count: int) -> dict[int, int]:
    """Map physical button index -> the number ``OnEvent`` reports, as G HUB numbers them.

    Left, right and middle are 1, 2 and 3; buttons labelled "(Gn)" get n; the
    rest follow in order after the highest number used.
    """
    out: dict[int, int] = {}
    for i in range(min(count, 3)):
        out[i] = i + 1
    for i in range(3, count):
        m = re.search(r"\(G(\d+)\)", names[i]) if i < len(names) else None
        if m:
            out[i] = int(m.group(1))
    used = set(out.values())
    nxt = max(used, default=0) + 1
    for i in range(count):
        if i not in out:
            while nxt in used:
                nxt += 1
            out[i] = nxt
            used.add(nxt)
    return out


# --- the sandbox ------------------------------------------------------------------------------
LUA_MODULES = ("lupa.lua54", "lupa.lua53", "lupa.lua51", "lupa.luajit21", "lupa")
_lupa_module = None


def lupa():
    """The lupa module to use (Lua 5.4 preferred), or None when lupa isn't installed."""
    global _lupa_module
    if _lupa_module is None:
        for name in LUA_MODULES:
            try:
                mod = importlib.import_module(name)
            except ImportError:
                continue
            if hasattr(mod, "LuaRuntime"):
                _lupa_module = mod
                break
        else:
            _lupa_module = False
    return _lupa_module or None


def available() -> bool:
    return lupa() is not None


MISSING = ("Lua scripts need the Python package 'lupa': run ./scripts/install.sh again (it installs it), "
           "or 'pip install lupa' when running from the source tree")

_ABORT = "__logi_ghub_script_aborted__"

# Runs with the full standard library and returns a function that builds a
# script environment from the host API table. The script only ever sees the
# environment; the API table and the Python functions in it stay upvalues.
_PRELUDE = r"""
local api, ABORT = ...
local select, error, type, tostring, tonumber, pairs, ipairs = select, error, type, tostring, tonumber, pairs, ipairs
local pcall, xpcall = pcall, xpcall
local fmt, concat = string.format, table.concat
local tunpack = table.unpack or unpack
local function pack(...) return {n = select("#", ...), ...} end
local function check(err) if err then error(err, 3) end end

local function copy(t) local c = {} for k, v in pairs(t) do c[k] = v end return c end

local env = {}
for _, k in ipairs({"assert", "error", "ipairs", "next", "pairs", "select", "tonumber", "tostring", "type",
                    "rawequal", "rawget", "rawset", "rawlen", "setmetatable", "getmetatable", "_VERSION"}) do
    env[k] = _G[k]
end
env.unpack = tunpack
env.string, env.table, env.math = copy(string), copy(table), copy(math)
local M, T = env.math, env.table
-- Lua 5.1 functions the API reference lists that newer Lua dropped.
M.pow = M.pow or function(x, y) return x ^ y end
M.log10 = M.log10 or function(x) return math.log(x, 10) end
M.ldexp = M.ldexp or function(m, e) return m * 2.0 ^ e end
M.frexp = M.frexp or function(x)
    if x == 0 then return 0.0, 0 end
    local e = math.floor(math.log(math.abs(x), 2)) + 1
    local m = x / 2.0 ^ e
    if math.abs(m) >= 1 then m, e = m / 2, e + 1 elseif math.abs(m) < 0.5 then m, e = m * 2, e - 1 end
    return m, e
end
M.sinh = M.sinh or function(x) return (math.exp(x) - math.exp(-x)) / 2 end
M.cosh = M.cosh or function(x) return (math.exp(x) + math.exp(-x)) / 2 end
M.tanh = M.tanh or function(x) local a, b = math.exp(x), math.exp(-x) return (a - b) / (a + b) end
T.maxn = T.maxn or function(t) local n = 0 for k in pairs(t) do if type(k) == "number" and k > n then n = k end end return n end
T.getn = T.getn or function(t) return #t end
-- An abort (profile switched off, script stopped) must not be swallowed by pcall.
local function rethrow(r)
    if not r[1] and api.aborted() then error(ABORT, 0) end
    return tunpack(r, 1, r.n)
end
env.pcall = function(...) return rethrow(pack(pcall(...))) end
env.xpcall = function(...) return rethrow(pack(xpcall(...))) end
env._G = env

local function logf(...) api.log(fmt(...)) end
env.OutputLogMessage = logf
env.OutputDebugMessage = function(...) api.debug(fmt(...)) end
env.print = function(...)
    local parts = {}
    for i = 1, select("#", ...) do parts[i] = tostring((select(i, ...))) end
    api.log(concat(parts, "\t") .. "\n")
end
env.ClearLog = function() api.clear_log() end
env.Sleep = function(ms) if api.sleep(tonumber(ms) or 0) then error(ABORT, 0) end end
env.GetRunningTime = function() return api.running_time() end
env.GetDate = function(f, t) return os.date(f, t) end

env.PressKey = function(...) check(api.keys(true, ...)) end
env.ReleaseKey = function(...) check(api.keys(false, ...)) end
env.PressAndReleaseKey = function(...) check(api.keys(true, ...)) check(api.keys(false, ...)) end
env.IsModifierPressed = function(name) return api.modifier_pressed(tostring(name)) end
env.IsKeyLockOn = function(name) return api.key_lock_on(tostring(name)) end

env.PressMouseButton = function(b) check(api.mouse_button(b, true)) end
env.ReleaseMouseButton = function(b) check(api.mouse_button(b, false)) end
env.PressAndReleaseMouseButton = function(b) check(api.mouse_button(b, true)) check(api.mouse_button(b, false)) end
env.IsMouseButtonPressed = function(b) return api.mouse_button_pressed(tonumber(b) or 0) end
env.MoveMouseTo = function(x, y) check(api.move_to(x, y, false)) end
env.MoveMouseToVirtual = function(x, y) check(api.move_to(x, y, true)) end
env.MoveMouseRelative = function(x, y) check(api.move_relative(x, y)) end
env.MoveMouseWheel = function(n) check(api.wheel(n)) end
env.GetMousePosition = function() return api.mouse_position() end
env.EnablePrimaryMouseButtonEvents = function(on) api.primary_events(on == true or on == 1) end

env.PlayMacro = function(name) check(api.play_macro(tostring(name))) end
env.AbortMacro = function() api.abort_macro() end
env.GetMKeyState = function(family) return api.get_mkey(family or "kb") end
env.SetMKeyState = function(m, family) check(api.set_mkey(m, family or "kb")) end
env.SetBacklightColor = function(r, g, b, family) check(api.set_backlight(r, g, b, family or "kb")) end
env.SetMouseDPITable = function(t, index)
    if type(t) ~= "table" then error("SetMouseDPITable: expected a table of DPI values", 2) end
    local v = {}
    for i = 1, #t do v[i] = t[i] end
    check(api.set_dpi_table(index or 1, tunpack(v)))
end
env.SetMouseDPITableIndex = function(i) check(api.set_dpi_index(i)) end
env.OutputLCDMessage = function(text, timeout) api.lcd(tostring(text), timeout or 1000) end
env.ClearLCD = function() api.lcd(nil, 0) end
for _, name in ipairs({"SetSteeringWheelProperty", "SetMouseSpeed", "IncrementMouseSpeed", "DecrementMouseSpeed"}) do
    env[name] = function() api.unsupported(name) end
end
env.GetMouseSpeed = function() api.unsupported("GetMouseSpeed") return 0 end

-- Stops runaway loops (`while true do end`) once the script is aborted.
if debug and debug.sethook then
    debug.sethook(function() if api.aborted() then error(ABORT, 0) end end, "", 1000)
end

local function compile(source)
    if setfenv then  -- Lua 5.1 / LuaJIT
        local f, err = loadstring(source, "=script")
        if f then setfenv(f, env) end
        return f, err
    end
    return load(source, "=script", "t", env)
end

local function onevent(event, arg, family)
    local f = env.OnEvent
    if type(f) ~= "function" then return "no OnEvent function" end
    f(event, arg, family)
end

return compile, onevent
"""


class ScriptError(Exception):
    """The script didn't compile or raised an error."""


class ScriptAborted(Exception):
    """The script was stopped while it was running."""


def _deny_attributes(obj, name, is_setting):
    raise AttributeError("not available to scripts")


class LuaScript:
    """A compiled script with its own Lua state. Use it from one thread only."""

    def __init__(self, source: str, host: "ScriptHost", *, max_memory: int = 64 << 20):
        mod = lupa()
        if mod is None:
            raise ScriptError(MISSING)
        self.host = host
        kwargs = dict(unpack_returned_tuples=True, register_eval=False, register_builtins=False,
                      attribute_filter=_deny_attributes)
        try:
            self.lua = mod.LuaRuntime(max_memory=max_memory, **kwargs)
        except TypeError:  # older lupa, or LuaJIT
            self.lua = mod.LuaRuntime(**kwargs)
        self.version = self.lua.eval("_VERSION")
        api = self.lua.table_from(host.lua_api())
        compile_fn, self._onevent = self.lua.execute(_PRELUDE, api, _ABORT)
        chunk, err = _pair(compile_fn(source))
        if chunk is None:
            raise ScriptError(str(err))
        self._call(chunk)  # top-level code: defines OnEvent, sets variables

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except mod_errors() as e:
            if type(e).__name__ == "LuaMemoryError":
                raise ScriptError("out of memory (a script may use up to 64 MB)") from None
            msg = str(e)
            if _ABORT in msg:
                raise ScriptAborted() from None
            raise ScriptError(_clean(msg)) from None

    def on_event(self, event: str, arg=None, family: str = "") -> None:
        err = self._call(self._onevent, event, arg, family)
        if err:
            raise ScriptError(str(err))


def _pair(result) -> tuple:
    # Lua functions return a bare value for one result and a tuple for several.
    return result if isinstance(result, tuple) else (result, None)


def mod_errors() -> tuple:
    mod = lupa()
    return (mod.LuaError, mod.LuaMemoryError) if hasattr(mod, "LuaMemoryError") else (mod.LuaError,)


def _clean(msg: str) -> str:
    # lupa appends a traceback for errors raised from Python callbacks; the first line is what matters.
    msg = msg.split("\nstack traceback:")[0]
    return msg.removeprefix("LuaError: ").strip()


def check(source: str) -> str | None:
    """Compile ``source`` without running it; return the error message, or None if it is fine."""
    mod = lupa()
    if mod is None:
        return MISSING
    lua = mod.LuaRuntime(register_eval=False, register_builtins=False, attribute_filter=_deny_attributes)
    compile_fn = lua.eval("function(s) if setfenv then return loadstring(s, '=script') end "
                          "return load(s, '=script', 't', {}) end")
    fn, err = _pair(compile_fn(source))
    return None if fn is not None else str(err)


# --- what the API acts on ------------------------------------------------------------------------
class ScriptHost:
    """The other side of the API. Keeps the state a script can query and records what it did.

    Subclasses override the ``out_*`` methods to send real input; the
    bookkeeping (held keys and buttons, M-key state, DPI table, pointer
    position) stays here so a stopped script can release everything it held.
    """

    def __init__(self, family: str = "mouse"):
        self.family = family
        self.actions: list[str] = []
        self.keys_down: list[int] = []  # evdev codes the script holds down
        self.buttons_down: list[int] = []  # Lua mouse button numbers the script holds down
        self.primary_events = False
        self.mkey = {"kb": 1, "lhc": 1}
        self.position = (32767, 32767)
        self.dpi_table: list[int] = []
        self.dpi_index = 0
        self.started = time.monotonic()
        self._unsupported: set[str] = set()
        self.abort_event = threading.Event()

    # -- for LuaScript ------------------------------------------------------------------
    def lua_api(self) -> dict:
        return {
            "log": self.log, "debug": self.debug, "clear_log": self.clear_log, "sleep": self._sleep,
            "running_time": lambda: int(self.now_ms()), "aborted": self.aborted,
            "keys": self._keys, "modifier_pressed": self.modifier_pressed, "key_lock_on": self.key_lock_on,
            "mouse_button": self._mouse_button, "mouse_button_pressed": self.mouse_button_pressed,
            "move_to": self._move_to, "move_relative": self._move_relative, "wheel": self._wheel,
            "mouse_position": lambda: self.position, "primary_events": self._primary,
            "play_macro": self._guard(self.play_macro), "abort_macro": self.abort_macro,
            "get_mkey": lambda family: self.mkey.get(str(family), 1), "set_mkey": self._set_mkey,
            "set_backlight": self._set_backlight, "set_dpi_table": self._set_dpi_table,
            "set_dpi_index": self._set_dpi_index, "lcd": self.lcd, "unsupported": self.unsupported,
        }

    @staticmethod
    def _guard(fn):
        def call(*args):
            try:
                fn(*args)
            except ValueError as e:
                return str(e)
            return None
        return call

    def _sleep(self, ms) -> bool:
        ms = max(0.0, float(ms))
        return self.sleep(ms)

    def _keys(self, down: bool, *names) -> str | None:
        fn = "PressKey" if down else "ReleaseKey"
        if not names:
            return f"{fn}: no key given"
        try:
            codes = [evdev_code(n) for n in names]
        except (ValueError, TypeError) as e:
            return f"{fn}: {e}"
        for code, name in zip(codes, names):
            if down:
                if code not in self.keys_down:
                    self.keys_down.append(code)
            elif code in self.keys_down:
                self.keys_down.remove(code)
            self.out_key(code, down, name)
        return None

    def _mouse_button(self, button, down: bool) -> str | None:
        try:
            b = int(button)
        except (TypeError, ValueError):
            b = 0
        if b not in LUA_BUTTON_TO_EVDEV:
            return f"mouse button must be 1-5, not {button!r}"
        if down and b not in self.buttons_down:
            self.buttons_down.append(b)
        elif not down and b in self.buttons_down:
            self.buttons_down.remove(b)
        self.out_mouse_button(b, down)
        return None

    def _move_to(self, x, y, virtual: bool) -> str | None:
        try:
            x, y = max(0, min(65535, int(x))), max(0, min(65535, int(y)))
        except (TypeError, ValueError):
            return "MoveMouseTo: x and y must be numbers from 0 to 65535"
        self.position = (x, y)
        self.out_move_to(x, y, virtual)
        return None

    def _move_relative(self, dx, dy) -> str | None:
        try:
            dx, dy = int(dx), int(dy)
        except (TypeError, ValueError):
            return "MoveMouseRelative: x and y must be numbers"
        self.out_move_relative(dx, dy)
        return None

    def _wheel(self, clicks) -> str | None:
        try:
            clicks = int(clicks)
        except (TypeError, ValueError):
            return "MoveMouseWheel: expected a number of clicks"
        if clicks:
            self.out_wheel(clicks)
        return None

    def _primary(self, on: bool) -> None:
        self.primary_events = bool(on)

    def _set_mkey(self, m, family) -> str | None:
        try:
            m = int(m)
        except (TypeError, ValueError):
            m = 0
        if m not in (1, 2, 3):
            return "SetMKeyState: M key must be 1, 2 or 3"
        self.mkey[str(family)] = m
        return None

    def _set_backlight(self, r, g, b, family) -> str | None:
        try:
            r, g, b = (max(0, min(255, int(v))) for v in (r, g, b))
        except (TypeError, ValueError):
            return "SetBacklightColor: red, green and blue must be numbers from 0 to 255"
        self.set_backlight((r << 16) | (g << 8) | b, str(family))
        return None

    def _set_dpi_table(self, index, *values) -> str | None:
        try:
            table = [int(v) for v in values]
            index = int(index)
        except (TypeError, ValueError):
            return "SetMouseDPITable: DPI values must be numbers"
        if not 1 <= len(table) <= 16:
            return "SetMouseDPITable: give 1 to 16 DPI values"
        if not 1 <= index <= len(table):
            return f"SetMouseDPITable: index must be 1-{len(table)}"
        self.dpi_table = table
        self.dpi_index = index - 1
        self.set_dpi(table, index - 1)
        return None

    def _set_dpi_index(self, index) -> str | None:
        table = self.dpi_table or self.current_dpi_table()
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = 0
        if not table or not 1 <= index <= len(table):
            return f"SetMouseDPITableIndex: index must be 1-{len(table) or 1}"
        self.dpi_table = table
        self.dpi_index = index - 1
        self.set_dpi(table, index - 1)
        return None

    # -- overridable ----------------------------------------------------------------------
    def now_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000

    def record(self, text: str) -> None:
        self.actions.append(text)

    def log(self, text: str) -> None:
        self.record(f"log: {text.rstrip()}")

    def debug(self, text: str) -> None:
        log.debug("script: %s", text.rstrip())

    def clear_log(self) -> None:
        self.record("ClearLog")

    def sleep(self, ms: float) -> bool:
        """Wait ``ms``; return True if the script was aborted meanwhile."""
        return self.abort_event.wait(ms / 1000)

    def aborted(self) -> bool:
        return self.abort_event.is_set()

    def out_key(self, code: int, down: bool, name) -> None:
        self.record(f"{'PressKey' if down else 'ReleaseKey'} {name}")

    def out_mouse_button(self, button: int, down: bool) -> None:
        self.record(f"{'PressMouseButton' if down else 'ReleaseMouseButton'} {button}")

    def out_move_to(self, x: int, y: int, virtual: bool) -> None:
        self.record(f"MoveMouseTo{'Virtual' if virtual else ''} {x} {y}")

    def out_move_relative(self, dx: int, dy: int) -> None:
        self.record(f"MoveMouseRelative {dx} {dy}")

    def out_wheel(self, clicks: int) -> None:
        self.record(f"MoveMouseWheel {clicks}")

    def modifier_pressed(self, name: str) -> bool:
        codes = MODIFIER_CODES.get(name.lower(), ())
        return any(c in self.keys_down for c in codes)

    def key_lock_on(self, name: str) -> bool:
        return False

    def mouse_button_pressed(self, button: int) -> bool:
        return button in self.buttons_down

    def play_macro(self, name: str) -> None:
        self.record(f"PlayMacro {name}")

    def abort_macro(self) -> None:
        self.record("AbortMacro")

    def set_backlight(self, rgb: int, family: str) -> None:
        self.record(f"SetBacklightColor {rgb:06x} {family}")

    def current_dpi_table(self) -> list[int]:
        return []

    def set_dpi(self, table: list[int], index: int) -> None:
        self.record(f"DPI {table[index]} (table {','.join(map(str, table))})")

    def lcd(self, text: str | None, timeout) -> None:
        self.record(f"LCD {text}" if text is not None else "ClearLCD")

    def unsupported(self, name: str) -> None:
        if name not in self._unsupported:
            self._unsupported.add(name)
            self.log(f"{name} is not supported on Linux (no such device); ignored\n")

    def error(self, text: str) -> None:
        self.log(f"[error] {text}\n")

    def release_all(self) -> None:
        """Let go of every key and button the script still holds."""
        for code in list(self.keys_down):
            self.out_key(code, False, code)
        self.keys_down.clear()
        for b in list(self.buttons_down):
            self.out_mouse_button(b, False)
        self.buttons_down.clear()


# --- running on a thread (daemon) ------------------------------------------------------------
class ScriptRunner:
    """Runs one profile's script on its own thread. ``post`` queues events for ``OnEvent``."""

    MAX_QUEUE = 256

    def __init__(self, source: str, host: ScriptHost, name: str = "script"):
        self.source = source
        self.host = host
        self.name = name
        self.events: queue.Queue = queue.Queue()
        self.abort = host.abort_event
        self.failed = False
        self.version = ""
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"lua:{name}", daemon=True)

    def start(self) -> "ScriptRunner":
        self.thread.start()
        return self

    def post(self, event: str, arg=None, family: str = "") -> None:
        if self.failed or not self.thread.is_alive():
            return
        if self.events.qsize() >= self.MAX_QUEUE:
            log.warning("%s: script is busy, dropping %s %s", self.name, event, arg)
            return
        self.events.put((event, arg, family))

    def _run(self) -> None:
        try:
            script = LuaScript(self.source, self.host)
            self.version = script.version
        except ScriptAborted:
            self.ready.set()
            return
        except ScriptError as e:
            self.failed = True
            self.host.error(str(e))
            log.warning("%s: %s", self.name, e)
            self.ready.set()
            return
        self.ready.set()
        while True:
            item = self.events.get()
            if item is None:
                break
            try:
                script.on_event(*item)
            except ScriptAborted:
                break
            except ScriptError as e:
                self.host.error(str(e))
                log.info("%s: %s", self.name, e)

    def stop(self, grace: float = 1.0) -> None:
        """Send PROFILE_DEACTIVATED, give the script ``grace`` seconds to finish, then abort it (as G HUB does)."""
        if self.thread.is_alive():
            self.post("PROFILE_DEACTIVATED")
            self.events.put(None)
            self.thread.join(grace)
            if self.thread.is_alive():
                self.abort.set()
                self.thread.join(1.0)
                if self.thread.is_alive():
                    log.warning("%s: script did not stop", self.name)
        self.host.release_all()


# --- dry runs ------------------------------------------------------------------------------------
@dataclass
class SimEvent:
    event: str
    arg: object = None
    family: str = "mouse"
    at_ms: float = 0.0


def parse_events(specs: list[str], family: str = "kb", gap_ms: float = 100.0) -> list[SimEvent]:
    """Parse 'EVENT[:ARG][@MS]' strings, e.g. 'MOUSE_BUTTON_PRESSED:1@0', 'G_PRESSED:3'.

    Events without a time follow the previous one after ``gap_ms``. G and M key
    events get ``family``; mouse events always come from "mouse".
    """
    out: list[SimEvent] = []
    t = 0.0
    for spec in specs:
        at = None
        if "@" in spec:
            spec, at_s = spec.rsplit("@", 1)
            at = float(at_s)
        event, _, arg = spec.partition(":")
        event = event.strip().upper()
        if event not in EVENTS:
            raise ValueError(f"unknown event {event!r}; one of {', '.join(EVENTS)}")
        value: object = None
        if arg.strip():
            value = int(arg) if arg.strip().lstrip("-").isdigit() else arg.strip()
        if at is None:
            at = t if not out else t + gap_ms
        t = at
        fam = "" if event.startswith("PROFILE_") else "mouse" if event.startswith("MOUSE_") else family
        out.append(SimEvent(event, value, fam, at))
    return sorted(out, key=lambda e: e.at_ms)


class SimHost(ScriptHost):
    """A virtual clock: Sleep advances time instantly, and button state follows the timed events."""

    def __init__(self, events: list[SimEvent], family: str = "mouse", limit_ms: float = 10_000,
                 real_limit_s: float = 3.0):
        super().__init__(family)
        self.timeline = events
        self.clock = 0.0
        self.limit_ms = limit_ms
        self.deadline = time.monotonic() + real_limit_s
        self.stopped = ""

    def now_ms(self) -> float:
        return self.clock

    def record(self, text: str) -> None:
        self.actions.append(f"{int(self.clock):>6} ms  {text}")

    def sleep(self, ms: float) -> bool:
        self.clock += ms
        return self.aborted()

    def aborted(self) -> bool:
        if self.clock > self.limit_ms:
            self.stopped = f"stopped after {self.limit_ms / 1000:g} s of simulated time"
        elif time.monotonic() > self.deadline:
            self.stopped = "stopped: still busy after a few seconds (a loop without Sleep?)"
        return bool(self.stopped)

    def mouse_button_pressed(self, button: int) -> bool:
        if super().mouse_button_pressed(button):
            return True
        down: set[int] = set()
        for e in self.timeline:
            if e.at_ms > self.clock:
                break
            if e.event.startswith("MOUSE_BUTTON_") and isinstance(e.arg, int):
                lua = {1: 1, 2: 3, 3: 2}.get(e.arg, e.arg)
                (down.add if e.event.endswith("PRESSED") else down.discard)(lua)
        return button in down


def simulate(source: str, events: list[SimEvent], family: str = "mouse", limit_ms: float = 10_000) -> tuple[list[str], str]:
    """Run ``source`` against timed events without sending input. Returns (transcript, Lua version)."""
    host = SimHost(events, family, limit_ms)
    try:
        script = LuaScript(source, host)
    except ScriptAborted:
        host.record(host.stopped)
        return host.actions, ""
    except ScriptError as e:
        host.record(f"error: {e}")
        return host.actions, ""
    for e in events:
        host.clock = max(host.clock, e.at_ms)
        if e.event == "MOUSE_BUTTON_PRESSED" and e.arg == 1 and not host.primary_events \
                or e.event == "MOUSE_BUTTON_RELEASED" and e.arg == 1 and not host.primary_events:
            host.record(f"({e.event} 1 not reported: call EnablePrimaryMouseButtonEvents(true))")
            continue
        host.record(f"OnEvent {e.event}" + (f" {e.arg}" if e.arg is not None else "") + (f" {e.family}" if e.family else ""))
        try:
            script.on_event(e.event, e.arg, e.family)
        except ScriptAborted:
            host.record(host.stopped or "aborted")
            break
        except ScriptError as err:
            host.record(f"error: {err}")
    host.release_all()
    return host.actions, script.version


# --- the script log (what G HUB shows under the script editor) -------------------------------
LOG_LIMIT = 256 * 1024


def log_path(model_key: str, profile_name: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", profile_name).strip("_") or "profile"
    d = data_dir() / "scripts" / model_key.replace(":", "-")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{slug}.log"


def append_log(path: Path, text: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
        if path.stat().st_size > LOG_LIMIT:
            data = path.read_bytes()[-LOG_LIMIT // 2:]
            path.write_bytes(data[data.find(b"\n") + 1:])
    except OSError as e:
        log.debug("script log: %s", e)


def read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
