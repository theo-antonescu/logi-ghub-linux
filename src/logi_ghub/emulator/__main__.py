"""Run virtual devices and drive them from stdin.

    sudo python3 -m logi_ghub.emulator [--backend uhid|gadget] [mouse] [keyboard]

Then type one command per line: ``mouse move 100 0``, ``mouse click right``, ``mouse scroll -2``,
``keyboard tap LeftCtrl C``, ``keyboard key_down LeftShift``, ``quit``.
"""

from __future__ import annotations

import argparse
import sys

from .devices import VirtualKeyboard, VirtualMouse

DEVICES = {"mouse": VirtualMouse, "keyboard": VirtualKeyboard}
COMMANDS = {"move", "scroll", "press", "release", "click", "key_down", "key_up", "tap"}


def run(devices: dict, lines) -> None:
    for line in lines:
        words = line.split()
        if not words:
            continue
        if words[0] == "quit":
            return
        name, command, args = *(words + ["", ""])[:2], words[2:]
        if name not in devices or command not in COMMANDS or not hasattr(devices[name], command):
            print(f"unknown command: {line.strip()}", file=sys.stderr)
            continue
        getattr(devices[name], command)(*(int(a) if a.lstrip("-").isdigit() else a for a in args))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["uhid", "gadget"], default="uhid")
    ap.add_argument("devices", nargs="*", help="mouse, keyboard or both (the default)")
    args = ap.parse_args(argv)
    devices = {name: DEVICES[name](args.backend) for name in args.devices or DEVICES}
    print(f"running {', '.join(devices)} ({args.backend}); type commands, or quit", file=sys.stderr)
    try:
        run(devices, sys.stdin)
    except KeyboardInterrupt:
        pass
    finally:
        for d in devices.values():
            d.close()


if __name__ == "__main__":
    main()
