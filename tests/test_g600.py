import pytest

from logi_ghub import profiles
from logi_ghub.actions import Action
from logi_ghub.features import g600


def mem():
    return g600.FakeG600Memory()


def test_parse_real_profiles():
    m = mem()
    p1, p2, p3 = m.read_profiles()
    assert p1.dpis[0] == 1200 and p1.report_rate_ms == 1
    assert p1.button(0) == Action.mouse(1)
    assert p1.button(5).label() == "G-Shift"
    assert p1.button(8).label() == "1"
    assert p1.button(8, gshift=True).label() == "Ctrl+1"
    assert p3.active_dpis == [400, 1200, 2000, 3200]
    assert m.get_current_profile() == 1


def test_round_trip_and_write():
    m = mem()
    p = m.read_profile(2)
    assert g600.G600Profile(p.to_bytes(), 2).raw == p.raw
    p.dpis = [800, 1600]
    p.set_button(6, Action.keypress("F13", ["LeftCtrl"]))
    p.set_led(1, 0xFF8800, 3)
    assert m.write_profile(p)
    back = m.read_profile(2)
    assert back.dpis == [800, 1600, 0, 0]
    assert back.button(6).label() == "Ctrl+F13"
    assert back.led["effect"] == 1 and back.led["color"] == 0xFF8800


def test_profile_conversion_keeps_shift_value_and_led():
    m = mem()
    prof = profiles.Profile.from_onboard("046d:c24a", m.read_profile(3), 20)
    assert prof.lighting["led"]["effect"] in (0, 1, 2)
    prof.shift_dpi = 600  # need not be a stage on the G600
    prof.dpi_stages = [400, 800, 1600, 3200, 6400]  # fifth stage is dropped
    changes = profiles.write_to_onboard(m, prof, 3, 20)
    p = m.read_profile(3)
    assert p.shift_dpi == 600 and p.active_dpis == [400, 800, 1600, 3200]
    assert changes


def test_unsupported_actions_are_refused():
    m = mem()
    prof = profiles.Profile.from_onboard("046d:c24a", m.read_profile(1), 20)
    prof.buttons[7] = Action.consumer("Mute")
    with pytest.raises(ValueError):
        profiles.write_to_onboard(m, prof, 1, 20)


def test_active_profile_switch():
    m = mem()
    m.set_current_profile(3)
    assert m.get_current_profile() == 3
