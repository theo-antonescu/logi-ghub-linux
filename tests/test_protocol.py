import pytest

from logi_ghub.hidpp import Device, DeviceOffline, FeatureNotSupported, HIDPPError
from logi_ghub.hidpp import features as F
from logi_ghub.hidpp.fake import FakeG502X, FakeTransport
from logi_ghub.hidpp.transport import _report_ids


def make():
    model = FakeG502X()
    t = FakeTransport(model)
    return Device(t), t, model


def test_ping_and_feature_table():
    dev, _, model = make()
    assert dev.ping() == (4, 2)
    feats = dev.discover_features()
    assert feats[F.ONBOARD_PROFILES] == model.index(F.ONBOARD_PROFILES)
    assert dev.has(F.ADJUSTABLE_DPI)
    with pytest.raises(FeatureNotSupported):
        dev.get_feature_index(F.RGB_EFFECTS)


def test_short_vs_long_reports():
    dev, t, _ = make()
    dev.call(F.ADJUSTABLE_DPI, 0x2, 0)
    assert t.log[-1][0] == 0x10 and len(t.log[-1]) == 7
    dev.call(F.ONBOARD_PROFILES, 0x5, 0, 1, 0, 0)
    assert t.log[-1][0] == 0x11 and len(t.log[-1]) == 20


def test_error_reply_raises():
    dev, _, _ = make()
    with pytest.raises(HIDPPError) as e:
        dev.call(F.ONBOARD_PROFILES, 0x5, 0x7F, 0x7F, 0, 0)
    assert e.value.code == 0x02


def test_notifications_are_dispatched_while_waiting():
    dev, t, model = make()
    seen = []
    dev.add_handler(seen.append)
    idx = dev.get_feature_index(F.MOUSE_BUTTON_SPY)
    t.notify(idx, 0, b"\x00\x08")
    dev.call(F.ADJUSTABLE_DPI, 0x0)
    assert seen and seen[0].sub_id == idx and seen[0].data[:2] == b"\x00\x08"


class OfflineTransport(FakeTransport):
    def write(self, data):
        self.queue.append(bytes([0x10, 0x01, 0x8F, data[2], data[3], 0x08, 0x00]))


def test_offline_device():
    dev = Device(OfflineTransport(FakeG502X()))
    with pytest.raises(DeviceOffline):
        dev.ping()


def test_report_descriptor_ids():
    # vendor collection with short (0x10) and long (0x11) HID++ reports
    desc = bytes.fromhex("06 00 ff 09 01 a1 01 85 10 75 08 95 06 15 00 26 ff 00 09 01 81 00 09 01 91 00 c0"
                         "06 00 ff 09 02 a1 01 85 11 75 08 95 13 09 02 81 00 09 02 91 00 c0")
    assert _report_ids(desc) == {0x10, 0x11}
