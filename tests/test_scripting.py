import re
import time

import pytest

from logi_ghub import profiles, scripting
from logi_ghub.daemon import MacroRunner, Session
from logi_ghub.device import _G502X_BUTTONS
from logi_ghub.hidpp import features as F
from logi_ghub.hidpp.fake import fake_devices
from logi_ghub.macros import Macro


def test_key_names_and_scancodes():
    assert scripting.evdev_code("a") == 30
    assert scripting.evdev_code(30) == scripting.evdev_code(0x1E) == 30
    assert scripting.evdev_code("I") == 23  # the reference spells it upper case
    assert scripting.evdev_code("lctrl") == 29 and scripting.evdev_code("rctrl") == 97
    assert scripting.evdev_code("f13") == 183 and scripting.evdev_code("f24") == 194
    assert scripting.evdev_code("numenter") == 96 and scripting.evdev_code("lgui") == 125
    with pytest.raises(ValueError):
        scripting.evdev_code("nope")


def test_g502x_button_numbers_follow_g_labels():
    nums = scripting.script_button_numbers(_G502X_BUTTONS, 11)
    assert nums == {0: 1, 1: 2, 2: 3, 3: 4, 4: 6, 5: 5, 6: 10, 7: 11, 8: 9, 9: 8, 10: 7}
    assert scripting.script_button_numbers([], 4) == {0: 1, 1: 2, 2: 3, 3: 4}


def test_profile_keeps_script():
    p = profiles.Profile(name="s", device="046d:409f", script="function OnEvent() end", script_enabled=False)
    q = profiles.Profile.from_dict(p.to_dict())
    assert q.script == p.script and q.script_enabled is False
    assert "script" not in profiles.Profile(name="n", device="046d:409f").to_dict()


def test_parse_events_times_and_families():
    ev = scripting.parse_events(["PROFILE_ACTIVATED", "MOUSE_BUTTON_PRESSED:4@50", "G_PRESSED:2"])
    assert [(e.event, e.arg, e.family, e.at_ms) for e in ev] == [
        ("PROFILE_ACTIVATED", None, "", 0), ("MOUSE_BUTTON_PRESSED", 4, "mouse", 50), ("G_PRESSED", 2, "kb", 150)]
    with pytest.raises(ValueError):
        scripting.parse_events(["KEY_PRESSED:1"])


# --- everything below runs Lua ------------------------------------------------------------------
lupa = pytest.importorskip("lupa")


def sim(src, events, **kw):
    actions, _ = scripting.simulate(src, scripting.parse_events(events), **kw)
    return [re.sub(r"^\s*\d+ ms  ", "", a) for a in actions]


def test_check_reports_syntax_errors():
    assert scripting.check("function OnEvent(event, arg) end") is None
    assert "script:1:" in scripting.check("function OnEvent(event, arg end")


def test_sandbox_has_only_the_documented_libraries():
    out = sim("print(io, os, require, debug, python, load, dofile, loadstring, coroutine)\n"
              "print(math.pow(2, 10), math.log10(100), table.maxn({1, 2, nil, 4}), string.upper('x'))\n"
              "print(#GetDate('%Y'), type(GetDate('*t')))", [])
    assert out[0] == "log: nil\tnil\tnil\tnil\tnil\tnil\tnil\tnil\tnil"
    assert out[1] == "log: 1024.0\t2.0\t4\tX"
    assert out[2] == "log: 4\ttable"


def test_keys_and_errors_carry_line_numbers():
    src = ('function OnEvent(event, arg)\n'
           '  PressAndReleaseKey("lctrl", 0x2e)\n'
           '  PressKey("nope")\n'
           'end\n')
    out = sim(src, ["G_PRESSED:1"])
    assert out[1:5] == ["PressKey lctrl", "PressKey 46", "ReleaseKey lctrl", "ReleaseKey 46"]
    assert out[5] == "error: script:3: PressKey: unknown key 'nope'"


def test_left_button_needs_enable_primary_events():
    src = 'function OnEvent(e, a) OutputLogMessage("%s %d\\n", e, a) end'
    assert "not reported" in sim(src, ["MOUSE_BUTTON_PRESSED:1"])[0]
    out = sim("EnablePrimaryMouseButtonEvents(true)\n" + src, ["MOUSE_BUTTON_PRESSED:1"])
    assert out[1] == "log: MOUSE_BUTTON_PRESSED 1"


def test_loop_until_button_released_on_virtual_clock():
    src = """
    EnablePrimaryMouseButtonEvents(true)
    function OnEvent(event, arg)
        if event == "MOUSE_BUTTON_PRESSED" and arg == 1 then
            repeat
                MoveMouseRelative(0, 3)
                Sleep(50)
            until not IsMouseButtonPressed(1)
        end
    end
    """
    out = sim(src, ["MOUSE_BUTTON_PRESSED:1@100", "MOUSE_BUTTON_RELEASED:1@300"])
    assert out.count("MoveMouseRelative 0 3") == 4


def test_runaway_loop_is_stopped_in_dry_runs():
    out = sim("function OnEvent() while true do Sleep(10) end end", ["G_PRESSED:1"], limit_ms=1000)
    assert "stopped after 1 s" in out[-1]


def test_dpi_table_and_backlight_are_recorded():
    out = sim('function OnEvent() SetMouseDPITable({400, 800, 1600}, 2) SetMouseDPITableIndex(3) '
              'SetBacklightColor(255, 0, 128) end', ["PROFILE_ACTIVATED"])
    assert out[1:] == ["DPI 800 (table 400,800,1600)", "DPI 1600 (table 400,800,1600)", "SetBacklightColor ff0080 kb"]


def test_memory_hog_is_an_error_not_a_crash():
    out = sim("function OnEvent() local t = {} for i = 1, 1e9 do t[i] = string.rep('x', 1e6) end end",
              ["PROFILE_ACTIVATED"])
    assert out[-1].startswith("error:")


def runner(src):
    host = scripting.ScriptHost()
    r = scripting.ScriptRunner(src, host, "test").start()
    r.ready.wait(2)
    return r, host


@pytest.mark.parametrize("body", ["while true do end", "while true do pcall(Sleep, 10) end",
                                  "while true do pcall(function() while true do end end) end"])
def test_runner_aborts_scripts_that_ignore_deactivation(body):
    r, host = runner(f"function OnEvent(e) if e == 'G_PRESSED' then {body} end end")
    r.post("G_PRESSED", 1, "kb")
    time.sleep(0.05)
    t0 = time.monotonic()
    r.stop(grace=0.2)
    assert not r.thread.is_alive()
    assert time.monotonic() - t0 < 2


def test_runner_releases_held_keys_on_stop():
    r, host = runner("function OnEvent(e) if e == 'G_PRESSED' then PressKey('lshift') PressMouseButton(3) end end")
    r.post("G_PRESSED", 1, "kb")
    time.sleep(0.1)
    r.stop()
    assert host.actions == ["PressKey lshift", "PressMouseButton 3", "ReleaseKey 42", "ReleaseMouseButton 3"]


def test_runner_reports_compile_errors():
    r, host = runner("function OnEvent(")
    assert r.failed and host.actions[0].startswith("log: [error] script:")


# --- in the daemon -------------------------------------------------------------------------------
class FakeInput:
    def __init__(self):
        self.events = []

    def key(self, code, down):
        self.events.append((code, down))

    def wheel(self, clicks, horizontal=False):
        self.events.append(("wheel", clicks))

    def move(self, dx, dy):
        self.events.append(("move", dx, dy))


def session(script, **kw):
    mouse = fake_devices()[0]
    prof = profiles.Profile(name="lua", device=mouse.key, script=script, **kw)
    vin = FakeInput()
    s = Session(mouse, prof, events=None, runner=MacroRunner(vin), vinput=vin)
    s.apply()
    mouse.hidpp.add_handler(s.on_message)
    s.script.ready.wait(2)
    return s, mouse, vin


def spy(mouse, mask):
    idx = mouse.hidpp.features[F.MOUSE_BUTTON_SPY]
    mouse.hidpp.transport.notify(idx, 0, mask.to_bytes(2, "big"))
    mouse.hidpp.poll(0.05)


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_script_types_on_button_press():
    s, mouse, vin = session('function OnEvent(event, arg, family)\n'
                            '  if event == "MOUSE_BUTTON_PRESSED" and arg == 4 and family == "mouse" then\n'
                            '    PressAndReleaseKey("a")\n'
                            '  end\n'
                            'end')
    spy(mouse, 1 << 3)  # Back (G4)
    spy(mouse, 0)
    assert wait_for(lambda: len(vin.events) >= 2)
    assert vin.events == [(30, True), (30, False)]
    s.release()
    assert s.script is None


def test_hold_left_button_loop_sees_real_button_state():
    s, mouse, vin = session("EnablePrimaryMouseButtonEvents(true)\n"
                            "function OnEvent(event, arg)\n"
                            "  if event == 'MOUSE_BUTTON_PRESSED' and arg == 1 then\n"
                            "    repeat MoveMouseRelative(0, 2) Sleep(20) until not IsMouseButtonPressed(1)\n"
                            "  end\n"
                            "end")
    spy(mouse, 1)
    time.sleep(0.2)
    spy(mouse, 0)
    time.sleep(0.1)
    moves = len(vin.events)
    time.sleep(0.1)
    assert moves >= 4 and len(vin.events) == moves  # moved while held, stopped after release
    assert set(vin.events) == {("move", 0, 2)}
    s.release()


def test_script_sets_dpi_and_buttons_use_its_table():
    s, mouse, _ = session("function OnEvent(e) if e == 'PROFILE_ACTIVATED' then SetMouseDPITable({500, 2500}, 2) end end",
                          dpi_stages=[400, 800, 1600], default_dpi=0)
    assert wait_for(lambda: mouse.fake_model.dpi == 2500)
    spy(mouse, 1 << 10)  # factory: DPI down
    spy(mouse, 0)
    assert mouse.fake_model.dpi == 500
    s.release()


def test_play_macro_from_script():
    s, mouse, vin = session("function OnEvent(e) if e == 'PROFILE_ACTIVATED' then PlayMacro('hi') end end",
                            macros=[Macro.parse("hi", '"hi"')])
    assert wait_for(lambda: len(vin.events) >= 4)
    assert vin.events == [(35, True), (35, False), (23, True), (23, False)]
    s.release()


def test_disabled_script_does_not_run():
    mouse = fake_devices()[0]
    prof = profiles.Profile(name="off", device=mouse.key, script="error('should not run')", script_enabled=False)
    s = Session(mouse, prof, events=None, runner=MacroRunner(None), vinput=None)
    s.apply()
    assert s.script is None
    s.release()


def test_script_log_file():
    s, mouse, _ = session("function OnEvent(e) OutputLogMessage('got %s\\n', e) end")
    s.release()  # sends PROFILE_DEACTIVATED and waits for it
    text = scripting.read_log(scripting.log_path(mouse.key, "lua"))
    assert "got PROFILE_ACTIVATED\ngot PROFILE_DEACTIVATED\n" in text


def test_mouse_script_colours_the_keyboard():
    mouse, kb = fake_devices()[:2]
    ms = Session(mouse, profiles.Profile(name="m", device=mouse.key, script=(
        "function OnEvent(e) if e == 'PROFILE_ACTIVATED' then SetBacklightColor(255, 0, 0) end end")),
        events=None, runner=MacroRunner(None), vinput=None)
    ks = Session(kb, profiles.Profile(name="k", device=kb.key), events=None, runner=MacroRunner(None), vinput=None)
    ms.peers = ks.peers = lambda: [ms, ks]
    ks.apply()
    ms.apply()
    assert wait_for(lambda: kb.fake_model.keys and set(kb.fake_model.keys.values()) == {0xFF0000})
    assert kb.fake_model.sw_control[0] != 0  # lighting claimed from the on-board effects
    ms.release()
    ks.release()
