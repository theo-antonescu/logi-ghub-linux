import json
import struct

import pytest

from logi_ghub.actions import Action
from logi_ghub.features import onboard as ob
from logi_ghub.hidpp import Device
from logi_ghub.hidpp.fake import FIXTURE, FakeG502X, FakeTransport


def dump():
    return json.loads(FIXTURE.read_text())


def test_crc_matches_device_sectors():
    for key, data in dump()["sectors"].items():
        b = bytes.fromhex(data)
        if int(key, 16) < 0x100:
            assert ob.sector_valid(b), key


def test_parse_g502x_profile():
    p = ob.OnboardProfile(bytes.fromhex(dump()["sectors"]["0x1"]), 1)
    assert p.report_rate_ms == 1
    assert p.dpis == [400, 800, 1000, 2400, 3000]
    assert p.default_dpi_index == 3
    assert p.name == "PROFILE_NAME_DEFAULT"
    assert p.button(0) == Action.mouse(1)
    assert p.button(3).label() == "Ctrl+X"
    assert p.button(4).label() == "G-Shift"
    assert p.button(5, gshift=True).label() == "Mute"
    assert p.button(10).kind == "none"
    assert p.to_bytes() == bytes.fromhex(dump()["sectors"]["0x1"])


def test_rom_profile_defaults():
    p = ob.OnboardProfile(bytes.fromhex(dump()["sectors"]["0x101"]), 0x101)
    labels = [p.button(i).label() for i in range(11)]
    assert labels == ["Left click", "Right click", "Middle click", "Back", "DPI shift (sniper)", "Forward",
                      "Scroll left", "Scroll right", "Cycle profile", "DPI up", "DPI down"]


def test_action_roundtrip():
    for a in [Action.mouse(4), Action.keypress("A", ["LeftCtrl", "LeftShift"]), Action.consumer("Mute"),
              Action.special("DPI cycle"), Action("none"), Action("default"), Action("macro", sector=6, address=0)]:
        assert Action.from_bytes(a.to_bytes()).to_bytes() == a.to_bytes()
        assert Action.from_dict(a.to_dict()) == a


def make():
    model = FakeG502X()
    dev = Device(FakeTransport(model))
    dev.discover_features()
    return ob.OnboardProfiles(dev), model


def test_read_profiles_and_directory():
    o, _ = make()
    assert o.info.sector_size == 255 and o.info.profile_count == 5
    assert o.read_directory() == [(1, True), (2, True), (3, True), (4, False), (5, False)]
    profiles = o.read_profiles()
    assert [p.dpis[3] for p in profiles[:2]] == [2400, 2600]


def test_write_profile_roundtrip():
    o, model = make()
    p = o.read_profiles()[3]
    p.dpis = [800, 1600, 3200]
    p.set_button(3, Action.keypress("F13"))
    p.name = "Linux"
    assert o.write_profile(p)
    assert model.writes == [4]
    again = o.read_profile(4)
    assert again.dpis == [800, 1600, 3200, 0, 0]
    assert again.button(3).label() == "F13"
    assert again.name == "Linux"
    assert not o.write_profile(again)  # unchanged: no write


def test_refuses_bad_crc_and_rom():
    o, _ = make()
    with pytest.raises(ValueError):
        o.write_sector(4, b"\x00" * 255)
    with pytest.raises(ValueError):
        o.write_sector(0x101, ob.seal(b"\xff" * 255))


def test_enable_slot_keeps_directory_format():
    o, model = make()
    o.set_enabled(4, True)
    d = bytes(model.memory[0])
    assert d[12:16] == bytes([0x00, 0x04, 0x01, 0xFF])
    assert ob.sector_valid(d)


def test_macro_encode_parse_and_write():
    o, model = make()
    ops = [(ob.OP_KEY_DOWN, bytes([0, 0x04])), (ob.OP_DELAY, struct.pack(">H", 50)), (ob.OP_KEY_UP, bytes([0, 0x04]))]
    free = o.free_macro_sectors(o.read_profiles())
    assert free[0] == 6
    o.write_macro(free[0], ops)
    assert o.read_macro(free[0], 0) == ops + [(ob.OP_END, b"")]


def test_backup_and_restore():
    o, model = make()
    b = o.backup()
    p = o.read_profiles()[0]
    p.dpis = [100]
    o.write_profile(p)
    assert o.restore(b) == [1]
    assert o.read_profile(1).dpis[0] == 400


def test_dpi_shift_is_stored_as_one_of_the_stages():
    p = ob.OnboardProfile(bytes.fromhex(dump()["sectors"]["0x1"]), 1)
    assert p.shift_dpi == p.dpis[p.shift_dpi_index] and p.max_dpi_stages == 5 and p.led is None
    p.set_shift_dpi(2400)
    assert p.shift_dpi_index == 3 and p.shift_dpi == 2400
    p.set_shift_dpi(0)  # 0: leave it
    assert p.shift_dpi == 2400
    with pytest.raises(ValueError):
        p.set_shift_dpi(1234)
    p.raw[2] = 7  # an index past the five stages
    assert p.shift_dpi is None
