import os
import struct

import pytest

from logi_ghub.emulator.backends import UHID_OUTPUT, UHID_SET_REPORT, UHid
from logi_ghub.emulator.devices import VirtualKeyboard, VirtualMouse
from logi_ghub.features import basic
from logi_ghub.features.inputs import MouseButtonSpy
from logi_ghub.hidpp import features as F
from logi_ghub.hidpp.protocol import Device, HIDPPError, Message


def connect(virtual):
    dev = Device(virtual.backend)
    dev.ping()
    dev.discover_features()
    return dev


def test_app_sees_a_g502x():
    dev = connect(VirtualMouse("loopback"))
    assert basic.device_name(dev) == "G502 X LIGHTSPEED"
    assert basic.model_ids(dev)[1] == [0x409F, 0xC098]
    assert [f.version for f in basic.firmware(dev)] == ["30.00.B0014", "48.00.B0014"]
    with pytest.raises(HIDPPError):
        dev.request(0x40, 0)


def test_button_spy_and_remap():
    mouse = VirtualMouse("loopback")
    dev = connect(mouse)
    spy = MouseButtonSpy(dev)
    spy.set_remapping([1, 2] + [0] * 9)  # back button goes only to the software
    spy.start()
    mouse.press("back")
    note, report = (mouse.backend.read(0) for _ in range(2))
    assert spy.parse(Message.parse(note)) == 1 << 3
    assert report[:3] == b"\x02\x00\x00"
    mouse.press("left")
    mouse.backend.read(0)
    assert mouse.backend.read(0)[1] == 0x01


def test_moves_and_keys():
    mouse, kb = VirtualMouse("loopback"), VirtualKeyboard("loopback")
    mouse.move(5, -3)
    assert struct.unpack("<BHhhbb", mouse.backend.read(0)) == (2, 0, 5, -3, 0, 0)
    kb.tap("LeftCtrl", "C")
    assert kb.backend.read(0) == bytes([1, 0x01, 0, 0x06, 0, 0, 0, 0, 0])
    assert kb.backend.read(0) == b"\x01" + bytes(8)
    assert basic.device_name(connect(kb)) == "G915 X LS TKL"


def test_uhid_events():
    got = []
    uhid = UHid.__new__(UHid)
    r, uhid.fd = os.pipe()
    uhid.on_output = got.append
    out = struct.pack("<I", UHID_OUTPUT) + b"\x10\xff\x00\x1b".ljust(4096, b"\x00") + struct.pack("<HB", 7, 1)
    uhid.handle(out)
    uhid.handle(struct.pack("<IIBBH", UHID_SET_REPORT, 9, 0x11, 0, 3) + b"\x11\xff\x01")
    assert got == [b"\x10\xff\x00\x1b\x00\x00\x00", b"\x11\xff\x01"]
    assert struct.unpack("<IIH", os.read(r, 10)) == (14, 9, 0)
