import os

from logi_ghub.features.g600 import enumerate_g600
from logi_ghub.hidpp.transport import enumerate_hidpp, hidraw_nodes

# vendor collection with short (0x10) and long (0x11) HID++ reports
HIDPP_DESC = bytes.fromhex("06 00 ff 09 01 a1 01 85 10 75 08 95 06 15 00 26 ff 00 09 01 81 00 09 01 91 00 c0"
                           "06 00 ff 09 02 a1 01 85 11 75 08 95 13 09 02 81 00 09 02 91 00 c0")
G600_DESC = bytes.fromhex("06 80 ff 09 80 a1 01 85 f0 75 08 95 03 09 f0 b1 02 c0")


def node(sysfs, n, hid_id, driver, desc, name="Logitech device"):
    dev = sysfs / "class" / "hidraw" / f"hidraw{n}" / "device"
    dev.mkdir(parents=True)
    (dev / "uevent").write_text(f"HID_ID={hid_id}\nHID_NAME={name}\nHID_PHYS=usb-0000:00:14.0-{n}/input2\n")
    (dev / "report_descriptor").write_bytes(desc)
    drv = sysfs / "drivers" / driver
    drv.mkdir(parents=True, exist_ok=True)
    os.symlink(drv, dev / "driver")


def test_discovery_walks_sysfs(tmp_path):
    node(tmp_path, 10, "0003:0000046D:0000409F", "logitech-hidpp-device", HIDPP_DESC, "G502 X")
    node(tmp_path, 2, "0003:0000046D:0000C547", "logitech-djreceiver", HIDPP_DESC)
    node(tmp_path, 3, "0003:0000046D:0000C24A", "hid-generic", G600_DESC, "G600")
    node(tmp_path, 4, "0003:00001234:00005678", "hid-generic", HIDPP_DESC)
    root = tmp_path / "class" / "hidraw"

    assert [n.name for n in hidraw_nodes(root)] == ["hidraw2", "hidraw3", "hidraw10"]  # numeric order, Logitech only
    (mouse,) = enumerate_hidpp(root)  # receiver and G600 skipped
    assert (mouse.path, mouse.name, mouse.key, mouse.behind_receiver) == ("/dev/hidraw10", "G502 X", "046d:409f", True)
    assert mouse.supports_short and mouse.supports_long
    (g600,) = enumerate_g600(root)
    assert (g600.path, g600.key, g600.driver) == ("/dev/hidraw3", "046d:c24a", "hid-generic")
