import pytest

from logi_ghub.actions import Action, parse_action
from logi_ghub.features import onboard as ob
from logi_ghub.macros import Macro, parse_chord


def test_parse_and_text_roundtrip():
    m = Macro.parse("copy-paste", 'Ctrl+C, 40ms, Ctrl+V, "Hi!", mouse:1, consumer:Mute')
    assert m.to_text() == 'LeftCtrl+C, 40ms, LeftCtrl+V, "Hi!", mouse:1, consumer:Mute'
    assert Macro.from_dict(m.to_dict()) == m


def test_events_expand_text_with_shift():
    m = Macro.parse("t", '"aB"')
    assert m.events() == [("key_down", "A"), ("key_up", "A"),
                          ("key_down", "LeftShift"), ("key_down", "B"), ("key_up", "B"), ("key_up", "LeftShift")]


def test_onboard_roundtrip():
    m = Macro.parse("m", "Ctrl+C, 25ms, Enter")
    ops = m.to_onboard_ops()
    assert ops[0] == (ob.OP_KEY_DOWN, bytes([0x01, 0x00]))
    assert ops[-1] == (ob.OP_END, b"")
    back = Macro.from_onboard_ops("m", ops)
    assert back.events() == m.events()


def test_chords():
    assert parse_chord("ctrl+shift+t") == (["LeftCtrl", "LeftShift"], "T")
    assert parse_chord("Shift") == ([], "LeftShift")
    with pytest.raises(ValueError):
        parse_chord("A+B")


def test_parse_action():
    assert parse_action("mouse:4") == Action.mouse(4)
    assert parse_action("key:Ctrl+Z").label() == "Ctrl+Z"
    assert parse_action("consumer:Play/Pause").usage == 0xCD
    assert parse_action("special:dpi cycle").function == 0x05
    assert parse_action("special:Select profile:2").data == 2
    assert parse_action("none").kind == "none"
