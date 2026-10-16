"""Raw hidraw access and discovery of Logitech HID++ capable devices.

Only the Linux hidraw character devices are used, so there is no dependency on
hidapi or pyudev. Device discovery walks /sys/class/hidraw.
"""

from __future__ import annotations

import errno
import os
import select
from dataclasses import dataclass
from pathlib import Path

LOGITECH_VID = 0x046D

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
REPORT_VERY_LONG = 0x12
REPORT_SIZES = {REPORT_SHORT: 7, REPORT_LONG: 20, REPORT_VERY_LONG: 64}

SYS_HIDRAW = Path("/sys/class/hidraw")

BUS_USB = 0x03
BUS_BLUETOOTH = 0x05


@dataclass(frozen=True)
class HidrawInfo:
    """A hidraw node that speaks HID++ to one Logitech device."""

    path: str
    name: str
    vid: int
    pid: int
    bus: int
    driver: str
    report_ids: frozenset[int]
    # Physical path of the USB interface, used to tell two receivers apart.
    phys: str

    @property
    def behind_receiver(self) -> bool:
        return self.driver == "logitech-hidpp-device"

    @property
    def supports_long(self) -> bool:
        return REPORT_LONG in self.report_ids

    @property
    def supports_short(self) -> bool:
        return REPORT_SHORT in self.report_ids

    @property
    def key(self) -> str:
        """Stable identifier for the device model, e.g. ``046d:409f``."""
        return f"{self.vid:04x}:{self.pid:04x}"


def report_ids(descriptor: bytes) -> frozenset[int]:
    """Collect the HID report IDs declared by a report descriptor.

    Walks the short items of the descriptor; Report ID is global item tag 0x85.
    """
    ids = set()
    i = 0
    n = len(descriptor)
    while i < n:
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item
            if i + 1 >= n:
                break
            i += 3 + descriptor[i + 1]
            continue
        size = prefix & 0x03
        size = 4 if size == 3 else size
        if prefix & 0xFC == 0x84 and size >= 1 and i + 1 < n:
            ids.add(descriptor[i + 1])
        i += 1 + size
    return frozenset(ids)


def _uevent(path: Path) -> dict[str, str]:
    try:
        text = path.read_text()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


@dataclass(frozen=True)
class HidrawNode:
    """One entry of /sys/class/hidraw, with what its uevent and report descriptor say."""

    name: str  # e.g. hidraw3
    bus: int
    vid: int
    pid: int
    driver: str
    report_ids: frozenset[int]
    uevent: dict[str, str]

    def info(self, dev_root: str = "/dev") -> HidrawInfo:
        return HidrawInfo(
            path=f"{dev_root}/{self.name}",
            name=self.uevent.get("HID_NAME", "Logitech device"),
            vid=self.vid,
            pid=self.pid,
            bus=self.bus,
            driver=self.driver,
            report_ids=self.report_ids,
            phys=self.uevent.get("HID_PHYS", ""),
        )


def _node_number(path: Path) -> int:
    suffix = path.name[6:]
    return int(suffix) if suffix.isdigit() else 0


def hidraw_nodes(sys_root: Path = SYS_HIDRAW, vid: int = LOGITECH_VID):
    """Yield the hidraw nodes of one vendor whose report descriptor can be read, in node order."""
    if not sys_root.exists():
        return
    for node in sorted(sys_root.iterdir(), key=_node_number):
        dev = node / "device"
        ue = _uevent(dev / "uevent")
        try:
            bus, node_vid, pid = (int(x, 16) for x in ue.get("HID_ID", "").split(":"))
        except ValueError:
            continue
        if node_vid != vid:
            continue
        try:
            driver = os.path.basename(os.readlink(dev / "driver"))
        except OSError:
            driver = ""
        try:
            ids = report_ids((dev / "report_descriptor").read_bytes())
        except OSError:
            continue
        yield HidrawNode(node.name, bus, node_vid, pid, driver, ids, ue)


def enumerate_hidpp(sys_root: Path = SYS_HIDRAW, dev_root: str = "/dev") -> list[HidrawInfo]:
    """Return hidraw nodes of Logitech devices that accept HID++ 2.0 reports.

    Receivers themselves are skipped: with the kernel's logitech-dj driver
    loaded every paired device gets its own hidraw node (driver
    ``logitech-hidpp-device``), and the driver rewrites the device index of
    reports written to it, so talking to that node is simplest.
    """
    return [
        node.info(dev_root)
        for node in hidraw_nodes(sys_root)
        if node.driver != "logitech-djreceiver" and node.report_ids & {REPORT_SHORT, REPORT_LONG}
    ]


class HidrawTransport:
    """Blocking read/write on a hidraw node with select()-based timeouts."""

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)

    def fileno(self) -> int:
        return self.fd

    def write(self, data: bytes) -> None:
        written = os.write(self.fd, data)
        if written != len(data):
            raise OSError(errno.EIO, f"short write to {self.path}")

    def read(self, timeout: float | None) -> bytes | None:
        """Read one input report, or return None after ``timeout`` seconds."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        try:
            return os.read(self.fd, 64)
        except BlockingIOError:
            return None

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
