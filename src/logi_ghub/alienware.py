"""Alienware monitor lighting (AW2725DF, AW3225QF), played in step with the keyboard.

The monitor's USB hub has a Microchip "Hub Controller" HID interface that passes DDC/CI messages
to the monitor. A colour is a Dell DDC/CI command (VCP 0xD0) aimed at one LED, or at all of them.
Protocol from OpenRGB's AW3225QF driver (Adam Honse); the AW2725DF (187c:1014) speaks the same.

Each message takes the monitor about 50 ms, so the monitor gets its own slow player that only
sends when a colour visibly changes.
"""

from __future__ import annotations

import glob
import logging
import os
import threading
import time

from .design import Design, FrameLoop

log = logging.getLogger(__name__)

VENDOR = 0x187C
MODELS = {0x1013: "Alienware AW3225QF", 0x1014: "Alienware AW2725DF"}
LEDS = {"Logo": 0x01, "Number": 0x02, "Power button": 0x08}
ALL = 0x0B
GAP = 0.05  # seconds the monitor needs between messages


def find() -> tuple[str, str] | None:
    """(hidraw path, model name) of a connected monitor, or None."""
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(node, "device", "uevent")) as f:
                hid_id = next(line for line in f if line.startswith("HID_ID="))
        except (OSError, StopIteration):
            continue
        _bus, vendor, product = hid_id.strip().split("=")[1].split(":")
        if int(vendor, 16) == VENDOR and int(product, 16) in MODELS:
            return "/dev/" + os.path.basename(node), MODELS[int(product, 16)]
    return None


def _colour_packet(led: int, rgb: int) -> bytes:
    p = bytearray(b"\xff" * 65)
    p[0:14] = bytes([0x00, 0x92, 0x37, 0x0A, 0x00, 0x51, 0x87, 0xD0, 0x04,
                     led, (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF, 0x64])
    checksum = 0x6E
    for b in p[5:14]:
        checksum ^= b
    p[14] = checksum
    return bytes(p)


class AlienwareMonitor:
    def __init__(self, path: str, name: str = "Alienware monitor"):
        self.name = name
        self.fd = os.open(path, os.O_RDWR)
        for packet in (bytes([0x00, 0x95, 0, 0, 0]) + b"\xff" * 60,
                       bytes([0x00, 0x92, 0x37, 0x08, 0x00, 0x51, 0x85, 0x01, 0xFE, 0x03, 0x00, 0x06, 0x40]) + b"\xff" * 52,
                       bytes([0x00, 0x93, 0x37, 0x12]) + b"\x00" * 61):
            self._write(packet)

    def _write(self, packet: bytes) -> None:
        os.write(self.fd, packet)
        time.sleep(GAP)

    def set(self, colours: list[int]) -> None:
        """Colours for the logo, the number and the power button, in that order."""
        if len(set(colours)) == 1:
            self._write(_colour_packet(ALL, colours[0]))
        else:
            for led, rgb in zip(LEDS.values(), colours):
                self._write(_colour_packet(led, rgb))

    def close(self) -> None:
        os.close(self.fd)


class MonitorLights(threading.Thread):
    """Plays a design on the monitor from the shared start time ``t0``; dark while ``dark`` is set."""

    def __init__(self, design: Design, t0: float, dark: threading.Event, fps: float = 4.0):
        super().__init__(name="monitor-lights", daemon=True)
        self.design, self.t0, self.dark, self.fps = design, t0, dark, fps
        self.stop_event = threading.Event()

    def run(self) -> None:
        from .caselight import _differs, key_strip

        found = find()
        if found is None:
            return
        try:
            monitor = AlienwareMonitor(*found)
        except OSError as e:
            log.warning("%s found but can't be opened (%s); is the udev rule installed?", found[1], e)
            return
        log.info("monitor lighting on %s", monitor.name)
        renderer = FrameLoop(self.design, None, round(1000 / self.fps))
        zones = key_strip(len(LEDS))
        last = None
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                frame = {} if self.dark.is_set() else renderer.frame(started - self.t0)
                colours = [frame.get(z, 0) for z in zones]
                if _differs(last, colours):
                    monitor.set(colours)
                    last = colours
                self.stop_event.wait(max(0.0, 1 / self.fps - (time.monotonic() - started)))
        except OSError as e:  # unplugged or switched off
            log.warning("monitor lighting stopped: %s", e)
        finally:
            monitor.close()

    def stop(self) -> None:
        self.stop_event.set()
