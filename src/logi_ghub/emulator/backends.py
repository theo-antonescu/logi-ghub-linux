"""Ways to put a virtual HID device on the system. Each backend calls ``on_output`` with every report
the host sends and has ``send`` for input reports.

* ``UHid``: the kernel's uhid. The device exists only on this computer (hidraw, libinput, this app).
* ``UsbGadget``: a USB device made with configfs + f_hid. With the ``dummy_hcd`` module it is plugged
  into this computer's own USB bus, so QEMU can pass it to a Windows VM running G HUB. On a board with
  a USB device port (Raspberry Pi Zero 2 W / 4) it is a real USB device for any computer.
* ``Loopback``: in memory, for tests. It is also a transport for ``hidpp.protocol.Device``.
"""

from __future__ import annotations

import collections
import os
import struct
import threading
import time
from pathlib import Path
from typing import Callable

OnOutput = Callable[[bytes], None]

# struct uhid_event (linux/uhid.h), packed
UHID_DESTROY, UHID_OUTPUT, UHID_GET_REPORT, UHID_GET_REPORT_REPLY = 1, 6, 9, 10
UHID_CREATE2, UHID_INPUT2, UHID_SET_REPORT, UHID_SET_REPORT_REPLY = 11, 12, 13, 14
UHID_EVENT_SIZE = 4380
BUS_USB = 0x03
EIO = 5


class Backend:
    def __init__(self):
        self.on_output: OnOutput = lambda data: None
        self._stop = threading.Event()

    def start(self, on_output: OnOutput) -> None:
        self.on_output = on_output
        threading.Thread(target=self._loop, name=type(self).__name__, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except OSError:
                if not self._stop.is_set():
                    raise
                return

    def poll(self) -> None:
        raise NotImplementedError

    def send(self, report: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self._stop.set()


class UHid(Backend):
    def __init__(self, name: str, vid: int, pid: int, descriptor: bytes, path: str = "/dev/uhid"):
        super().__init__()
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        phys = f"logi-emulator/{vid:04x}:{pid:04x}".encode()
        os.write(self.fd, struct.pack("<I128s64s64sHHIIII4096s", UHID_CREATE2, name.encode(), phys, b"",
                                      len(descriptor), BUS_USB, vid, pid, 0x0100, 0, descriptor))

    def poll(self) -> None:
        self.handle(os.read(self.fd, UHID_EVENT_SIZE))

    def handle(self, ev: bytes) -> None:
        kind = struct.unpack_from("<I", ev)[0]
        if kind == UHID_OUTPUT:
            size = struct.unpack_from("<H", ev, 4100)[0]
            self.on_output(ev[4 : 4 + size])
        elif kind == UHID_SET_REPORT:  # an output report sent as a SET_REPORT control request
            req, _, _, size = struct.unpack_from("<IBBH", ev, 4)
            os.write(self.fd, struct.pack("<IIH", UHID_SET_REPORT_REPLY, req, 0))
            self.on_output(ev[12 : 12 + size])
        elif kind == UHID_GET_REPORT:  # Logitech devices have no feature reports to read
            req = struct.unpack_from("<I", ev, 4)[0]
            os.write(self.fd, struct.pack("<IIHH", UHID_GET_REPORT_REPLY, req, EIO, 0))

    def send(self, report: bytes) -> None:
        os.write(self.fd, struct.pack("<IH", UHID_INPUT2, len(report)) + report)

    def close(self) -> None:
        super().close()
        os.write(self.fd, struct.pack("<I", UHID_DESTROY))
        os.close(self.fd)


class UsbGadget(Backend):
    ROOT = Path("/sys/kernel/config/usb_gadget")

    def __init__(self, name: str, vid: int, pid: int, descriptor: bytes, udc: str | None = None,
                 serial: str = "0001"):
        super().__init__()
        self.dir = g = self.ROOT / f"logi-{pid:04x}"
        fn, cfg = g / "functions/hid.usb0", g / "configs/c.1"
        for d in (g, g / "strings/0x409", fn, cfg, cfg / "strings/0x409"):
            d.mkdir(parents=True, exist_ok=True)
        self._write(g, idVendor=f"{vid:#06x}", idProduct=f"{pid:#06x}", bcdDevice="0x0100", bcdUSB="0x0200")
        self._write(g / "strings/0x409", manufacturer="Logitech", product=name, serialnumber=serial)
        self._write(cfg, MaxPower="500")
        self._write(cfg / "strings/0x409", configuration=name)
        self._write(fn, protocol="0", subclass="0", report_length="64")
        (fn / "report_desc").write_bytes(descriptor)
        if not (cfg / "hid.usb0").exists():
            (cfg / "hid.usb0").symlink_to(fn)
        (g / "UDC").write_text(udc or self.free_udc())
        major, minor = map(int, (fn / "dev").read_text().split(":"))
        self.fd = self._open_hidg(os.makedev(major, minor))

    @staticmethod
    def _write(d: Path, **attrs: str) -> None:
        for k, v in attrs.items():
            (d / k).write_text(v)

    @classmethod
    def free_udc(cls) -> str:
        used = {(g / "UDC").read_text().strip() for g in cls.ROOT.iterdir()}
        free = [u for u in sorted(os.listdir("/sys/class/udc")) if u not in used]
        if not free:
            raise RuntimeError("no free USB device controller: load dummy_hcd (sudo modprobe dummy_hcd num=2)")
        return free[0]

    @staticmethod
    def _open_hidg(rdev: int) -> int:
        for _ in range(50):  # udev creates the node a moment after the gadget is bound
            for n in Path("/dev").glob("hidg*"):
                if n.stat().st_rdev == rdev:
                    return os.open(n, os.O_RDWR | os.O_CLOEXEC)
            time.sleep(0.1)
        raise FileNotFoundError("the gadget's /dev/hidgN node did not appear")

    def poll(self) -> None:
        self.on_output(os.read(self.fd, 64))

    def send(self, report: bytes) -> None:
        os.write(self.fd, report)

    def close(self) -> None:
        super().close()
        os.close(self.fd)
        g = self.dir
        (g / "UDC").write_text("\n")
        (g / "configs/c.1/hid.usb0").unlink()
        for d in ("configs/c.1/strings/0x409", "configs/c.1", "functions/hid.usb0", "strings/0x409", "."):
            (g / d).rmdir()


class Loopback(Backend):
    """The host side is this object: ``write`` sends an output report, ``read`` returns input reports."""

    def __init__(self, *_args, **_kwargs):
        super().__init__()
        self.path = "loopback"
        self.queue: collections.deque[bytes] = collections.deque()

    def start(self, on_output: OnOutput) -> None:
        self.on_output = on_output

    def send(self, report: bytes) -> None:
        self.queue.append(bytes(report))

    def fileno(self) -> int:
        return -1

    def write(self, data: bytes) -> None:
        self.on_output(bytes(data))

    def read(self, timeout):
        return self.queue.popleft() if self.queue else None


BACKENDS = {"uhid": UHid, "gadget": UsbGadget, "loopback": Loopback}
