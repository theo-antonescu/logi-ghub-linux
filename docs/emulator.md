# Virtual G502 X and G915 X TKL

`src/logi_ghub/emulator` makes a fake **G502 X LIGHTSPEED** mouse and **G915 X LS TKL** keyboard that
look like the real ones plugged in by USB cable. They answer HID++ 2.0 like the simulated devices
behind `--fake` (name, firmware, battery, DPI, report rate, on-board profiles, Mouse Button Spy,
lighting, brightness), and a Python API makes them move, click and type, so macros and button
assignments can be tested without touching the real hardware.

There are two ways to plug them in:

| Backend | Who sees the device | Needs |
|---|---|---|
| `uhid` (default) | Programs on this computer: this app, Solaar, the desktop | `/dev/uhid` (run with `sudo`) |
| `gadget` | A Windows VM running G HUB (passed through like a real USB device), or any computer when run on a Raspberry Pi | the `dummy_hcd` kernel module and configfs (run with `sudo`) |

## On this computer (uhid)

```sh
sudo PYTHONPATH=src python3 -m logi_ghub.emulator            # both devices
sudo PYTHONPATH=src python3 -m logi_ghub.emulator mouse      # just the mouse
```

Then type commands, one per line:

```
mouse move 100 0
mouse click right
mouse scroll -2
keyboard tap LeftCtrl C
keyboard key_down LeftShift
quit
```

Start the app (`PYTHONPATH=src python3 -m logi_ghub.gui.app`, without `--fake`) and the two devices
show up like real ones. The udev rule in `data/udev` (installed with `scripts/install.sh --udev`)
lets the app open them without root.

## In a Windows VM with G HUB (USB gadget)

```sh
sudo modprobe libcomposite
sudo modprobe dummy_hcd num=2          # one virtual USB port per device
sudo PYTHONPATH=src python3 -m logi_ghub.emulator --backend gadget
```

`lsusb` now lists `046d:c098` (mouse) and `046d:c357` (keyboard). Pass them into the VM from
`tools/ghub-capture` (the capture rule there lets you do it without root):

```sh
tools/ghub-capture/ghub-vm.sh capture 046d:c098 046d:c357
```

This also records every packet G HUB sends them, which is the quickest way to see what it asks
for that the fake devices don't answer yet (`python3 -m logi_ghub.tools.pcap FILE --summary`).

If `modprobe dummy_hcd` says the module is not found, your kernel was built without it
(`CONFIG_USB_DUMMY_HCD`), and it has to be built from the kernel source for your version. On a
Raspberry Pi Zero 2 W or Pi 4 the same command works over the board's USB device port, with
`dtoverlay=dwc2` instead of `dummy_hcd`.

## From Python

```python
from logi_ghub.emulator import VirtualKeyboard, VirtualMouse

with VirtualMouse() as mouse, VirtualKeyboard() as kb:   # VirtualMouse("gadget") for the VM
    mouse.move(200, -50)
    mouse.click("left")                 # "left", "right", "middle", "back", "forward" or 1-16
    mouse.press("back"); mouse.release("back")
    mouse.scroll(-3)
    kb.tap("LeftCtrl", "Z")             # key names from logi_ghub/keys.py
    kb.tap("Mute")                      # media keys work too
    print(mouse.model.dpi)              # what the software set, e.g. after a DPI change
```

`mouse.model` and `kb.model` hold the device state (DPI, report rate, on-board memory, lighting), so a
test can check what the software wrote. When the software turns on Mouse Button Spy (host-mode
macros in this app), button presses are sent to it as HID++ events and the HID mouse report uses
its remapping, like the real mouse.

## Limits

* The devices act as if they are on a USB cable, not on a Lightspeed receiver.
* `046d:c098` for the G502 X on a cable is a best guess; check it with `lsusb` on the real mouse and
  change `G502XModel.usb_pid` if it differs.
* G HUB may ask for HID++ features the fake devices don't have (for example 0x1D4B, 0x2121, 0x8101)
  or check firmware details, and then not show a device or some of its pages. The recording shows
  which.
* Each device uses a single USB interface with all its reports. The real ones split them over
  several interfaces; G HUB on Windows should still find the HID++ part.
