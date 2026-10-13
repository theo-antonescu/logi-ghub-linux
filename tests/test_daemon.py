from logi_ghub import profiles
from logi_ghub.actions import Action
from logi_ghub.daemon import MacroRunner, Session
from logi_ghub.hidpp import features as F
from logi_ghub.hidpp.fake import fake_devices
from logi_ghub.macros import Macro


class FakeInput:
    def __init__(self):
        self.events = []

    def key(self, code, down):
        self.events.append((code, down))

    def wheel(self, clicks, horizontal=False):
        self.events.append(("wheel", clicks, horizontal))


def session(profile_kwargs):
    mouse = fake_devices()[0]
    prof = profiles.Profile(name="t", device=mouse.key, **profile_kwargs)
    vin = FakeInput()
    s = Session(mouse, prof, events=None, runner=MacroRunner(vin), vinput=vin)
    s.apply()
    return s, mouse, vin


def spy(s, mouse, mask):
    idx = mouse.hidpp.features[F.MOUSE_BUTTON_SPY]
    mouse.hidpp.transport.notify(idx, 0, mask.to_bytes(2, "big"))
    mouse.hidpp.poll(0.05)


def collect(s, mouse):
    mouse.hidpp.add_handler(s.on_message)


def test_apply_sets_host_mode_dpi_and_remap():
    s, mouse, _ = session({"dpi_stages": [400, 800, 1600], "default_dpi": 1, "shift_dpi": 400,
                           "buttons": {3: Action.keypress("C", ["LeftCtrl"])}})
    m = mouse.fake_model
    assert m.mode == 2 and m.dpi == 800 and m.spying
    assert m.remap[0:3] == [1, 2, 3]  # factory left/right/middle stay native
    assert m.remap[3] == 0  # remapped button is silenced and handled by the daemon


def test_key_action_follows_press_and_release():
    s, mouse, vin = session({"buttons": {3: Action.keypress("C", ["LeftCtrl"])}})
    collect(s, mouse)
    spy(s, mouse, 1 << 3)
    spy(s, mouse, 0)
    assert vin.events == [(29, True), (46, True), (46, False), (29, False)]


def test_dpi_buttons_and_shift_use_factory_defaults():
    s, mouse, _ = session({"dpi_stages": [400, 800, 1600], "default_dpi": 1, "shift_dpi": 400})
    collect(s, mouse)
    m = mouse.fake_model
    spy(s, mouse, 1 << 9)  # factory: DPI up
    spy(s, mouse, 0)
    assert m.dpi == 1600
    spy(s, mouse, 1 << 4)  # factory: DPI shift held
    assert m.dpi == 400
    spy(s, mouse, 0)
    assert m.dpi == 1600


def test_gshift_layer():
    s, mouse, vin = session({"buttons": {4: Action.special("G-Shift"), 5: Action.keypress("A")},
                             "gshift_buttons": {5: Action.consumer("Mute")}})
    collect(s, mouse)
    spy(s, mouse, 1 << 4)
    spy(s, mouse, (1 << 4) | (1 << 5))
    spy(s, mouse, 1 << 4)
    spy(s, mouse, 0)
    assert vin.events == [(113, True), (113, False)]


def test_release_restores_onboard_mode():
    s, mouse, _ = session({})
    s.release()
    m = mouse.fake_model
    assert m.mode == 1 and not m.spying and m.remap == list(range(1, 12))


def test_macro_runs():
    import time

    s, mouse, vin = session({"buttons": {3: Action.run_macro("hi")}, "macros": [Macro.parse("hi", '"hi"')]})
    collect(s, mouse)
    spy(s, mouse, 1 << 3)
    spy(s, mouse, 0)
    time.sleep(0.1)
    assert vin.events == [(35, True), (35, False), (23, True), (23, False)]
