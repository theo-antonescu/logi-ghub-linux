"""Features that let the host see and replace button presses (host mode).

* 0x8110 Mouse Button Spy: the mouse reports every physical button as a
  bitmask notification, and a remapping table decides which HID button each
  physical button still sends (0 = send nothing). G HUB uses this in host mode.
* 0x8010 G-keys: keyboards report G-key presses as a bitmask notification once
  software control is enabled.
"""

from __future__ import annotations

import struct

from ..hidpp import Device, Message
from ..hidpp import features as F


class MouseButtonSpy:
    def __init__(self, dev: Device):
        self.dev = dev
        self.index = dev.get_feature_index(F.MOUSE_BUTTON_SPY)
        self.count = dev.call(F.MOUSE_BUTTON_SPY, 0x0)[0]

    def start(self) -> None:
        self.dev.call(F.MOUSE_BUTTON_SPY, 0x1)

    def stop(self) -> None:
        self.dev.call(F.MOUSE_BUTTON_SPY, 0x2)

    def get_remapping(self) -> list[int]:
        return list(self.dev.call(F.MOUSE_BUTTON_SPY, 0x3)[: self.count])

    def set_remapping(self, mapping: list[int]) -> None:
        """``mapping[i]`` is the HID button (1-based) physical button i+1 sends, 0 for none."""
        self.dev.call(F.MOUSE_BUTTON_SPY, 0x4, bytes(mapping[:16]).ljust(16, b"\x00"))

    def identity(self) -> list[int]:
        return list(range(1, self.count + 1))

    def parse(self, msg: Message) -> int | None:
        """Return the pressed-button bitmask (bit 0 = physical button 1) from a spy notification."""
        if msg.sub_id != self.index or msg.function != 0 or msg.swid != 0:
            return None
        return struct.unpack(">H", msg.data[0:2])[0]


class GKeys:
    def __init__(self, dev: Device):
        self.dev = dev
        self.index = dev.get_feature_index(F.GKEY)
        self.count = dev.call(F.GKEY, 0x0)[0]

    def enable_software_control(self, enable: bool = True) -> None:
        self.dev.call(F.GKEY, 0x2, 0x01 if enable else 0x00)

    def parse(self, msg: Message) -> int | None:
        if msg.sub_id != self.index or msg.function != 0 or msg.swid != 0:
            return None
        return struct.unpack("<I", msg.data[0:4])[0]


def bitmask_changes(old: int, new: int, width: int = 32) -> tuple[list[int], list[int]]:
    """Return (pressed, released) bit numbers, 0-based."""
    pressed = [i for i in range(width) if new & (1 << i) and not old & (1 << i)]
    released = [i for i in range(width) if old & (1 << i) and not new & (1 << i)]
    return pressed, released
