# logi-ghub-linux

A Linux replacement for Logitech G HUB. It configures Logitech G mice and
keyboards over HID++ 2.0: DPI, report rate, button remapping, macros, lighting,
and the profiles stored in the device's on-board memory. It ships a GTK 4 /
libadwaita app, a command-line tool and a small background service.

Developed and tested on Ubuntu 26.04 with a **G502 X LIGHTSPEED** mouse and a
**G915 X LIGHTSPEED TKL** keyboard (each on its own Lightspeed receiver or by
USB cable), and a corded **G600** MMO mouse.

| | G502 X LIGHTSPEED | G915 X LIGHTSPEED TKL |
|---|---|---|
| Battery, firmware info | yes | yes |
| DPI stages, DPI shift, report rate | yes, on-board and host mode | n/a |
| Button remapping (keys, media keys, mouse buttons, DPI/profile functions, G-Shift layer) | yes, on-board and host mode | not yet (see Limitations) |
| Macros | host mode (played by the service, with repeat modes); on-board is experimental | not yet |
| Lighting: zone effects, per-key colours, brightness, your own colour sequences | the LIGHTSPEED model has no RGB | yes, host mode |
| On-board profile slots: read, edit, enable, switch, back up, restore | yes | n/a (uses a newer, undocumented feature) |

The **G600** is older and doesn't speak HID++ 2.0; it is configured through HID
feature reports. Its three on-board profiles (20 buttons plus a G-Shift layer,
four DPI stages, DPI shift, polling rate, LED colour/effect and G-Shift colour)
can be read, edited, backed up and written. It has no macro memory and no host
mode, so macros and colour sequences are not available on it.

Other HID++ 2.0 Logitech gaming mice that use the same on-board memory format
(most G mice from the last decade) should mostly work too; unknown models just
show generic button names.

## Install

Requirements (already present on a standard Ubuntu desktop): Python 3.11+,
`python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`.

```sh
git clone https://github.com/theo-antonescu/logi-ghub-linux
cd logi-ghub-linux
./scripts/install.sh --udev                # --udev installs the device-access rule (sudo)
systemctl --user enable --now logi-ghubd   # only needed for host-mode profiles
```

The udev rule (`data/udev/70-logi-ghub-linux.rules`) gives the logged-in user
access to Logitech hidraw nodes and to `/dev/uinput`, so nothing runs as root.
The installer puts the app in `~/.local/share/logi-ghub-linux/venv`, links
`logi-ghub`, `logi-ghub-cli` and `logi-ghubd` into `~/.local/bin`, and adds a
"Logitech G devices" launcher. `./scripts/install.sh --uninstall` removes it.

To try it without installing: `PYTHONPATH=src python3 -m logi_ghub.gui.app`.
Add `--fake` to the app or CLI to use simulated devices.

## On-board memory vs. host mode

Like G HUB, there are two ways a profile can run:

* **On-board**: the profile is written into one of the device's memory slots.
  It works with no software running, on any computer. The G502 X has 5 slots;
  macros are stored in the spare memory sectors. Before every write the whole
  on-board memory is saved to `~/.local/share/logi-ghub-linux/backups/`, and
  every written sector is read back and checked. *Profiles → Restore a backup*
  (or `logi-ghub-cli onboard restore FILE --yes`) puts a backup back.
* **Host mode**: a profile saved on this computer is applied by the
  `logi-ghubd` service. The device is switched out of on-board mode; the
  service watches button presses (HID++ Mouse Button Spy) and performs the
  actions through a virtual input device, so macros can repeat while held or
  toggle, and lighting is driven from the computer (switched off when the
  keyboard reports you idle, back on when you return). When the service stops
  or the profile is deactivated, the device goes back to its on-board profile.

## The app

* **Profiles**: the on-board slots (activate, edit), profiles saved on this
  computer (edit, run in host mode, write into a slot, delete), backups.
* **Sensitivity**: up to 5 DPI stages, the default stage, DPI shift, polling rate.
  The play button tries a DPI immediately.
* **Assignments**: every button and its G-Shift second function. Keystrokes can
  be captured by pressing them.
* **Macros**: write steps as text or record them by typing. Macros can play
  once, repeat while the button is held, or repeat until pressed again, with a
  pause between repeats and keys held down the whole time (for example: hold
  Alt and tap Tab every 500 ms, for a button that cycles windows). Repeating
  and held keys run in host mode.
* **Lighting**: a live picture of the keyboard that animates the way the keys
  do; paint keys by clicking or dragging; zone effects (static, breathing, colour
  cycle, colour wave, ripple); brightness; and colour sequences: your own list
  of colours in your order, stepping or fading, across the whole keyboard or as
  a wave (right, left, down, up, from the centre), optionally keeping painted
  keys. Sequences are played by the computer (the app while it is open, or the
  service in host mode).
* **Device**: firmware, battery, the HID++ feature table.

Edits are made to a working copy; the banner at the top saves them to the slot
or file being edited.

## Command line

```sh
logi-ghub-cli list
logi-ghub-cli -d mouse info
logi-ghub-cli -d mouse onboard show
logi-ghub-cli -d mouse onboard backup
logi-ghub-cli -d mouse onboard set --slot 2 --dpi 800,1600,3200 --default-dpi 2 --shift-dpi 800 \
    --button '4=key:Ctrl+C' --button '6=consumer:Mute' --gshift '6=consumer:Play/Pause' \
    --macro 'hello="Hello", Enter' --button '10=macro:hello' --yes
logi-ghub-cli -d mouse onboard select 2
logi-ghub-cli -d mouse dpi 1200
logi-ghub-cli -d keyboard lighting effect Breathing --color ff0000 --period 3000
logi-ghub-cli -d keyboard lighting keys --all 000000 W=ff0000 A=ff0000 S=ff0000 D=ff0000
logi-ghub-cli -d keyboard lighting sequence ff0000,ffa000,00ff00,0000ff --step 700 --pattern wave_right
logi-ghub-cli -d keyboard lighting release
logi-ghub-cli -d mouse profile import --slot 1 --name Gaming
logi-ghub-cli -d mouse profile activate Gaming     # host mode via logi-ghubd ('none' to stop)
logi-ghub-cli probe --out data/devices             # read-only feature dump for new models
```

Action syntax: `default`, `none`, `mouse:N`, `key:Ctrl+Shift+T`,
`consumer:<media key>`, `special:<function>` (DPI up, DPI down, DPI cycle,
DPI shift (sniper), Cycle profile, G-Shift, Scroll left, ...), `macro:NAME`.
Macro syntax: comma separated `Ctrl+C`, `50ms`, `down:Shift`, `up:Shift`,
`"text"`, `mouse:1`, `consumer:Mute`.

## How it works

Everything is plain HID++ 2.0 over `/dev/hidraw*`, with no dependencies beyond
the Python standard library (plus PyGObject for the GUI). Devices behind a
Lightspeed/Unifying receiver are reached through the per-device hidraw node the
kernel's `logitech-hidpp` driver creates.

| Feature | Used for |
|---|---|
| 0x0001, 0x0003, 0x0005, 0x1004 | feature table, firmware, name, battery |
| 0x2201 Adjustable DPI | DPI list and current DPI |
| 0x8060 / 0x8061 Report rate | polling rate |
| 0x8100 Onboard Profiles | slots, buttons, DPI, macros in device memory |
| 0x8110 Mouse Button Spy | host-mode button capture and remapping |
| 0x8071 RGB Effects | zone effects, software lighting control, idle power |
| 0x8081 Per-key Lighting v2 | per-key colours |
| 0x8040 Brightness Control | keyboard brightness |
| 0x8101 Profile Management | taking lighting over on newer keyboards |

`data/devices/*.json` holds read-only probes of the two development devices.
`src/logi_ghub/hidpp/fake.py` simulates both (seeded with a real G502 X memory
dump) for the tests: `python3 -m pytest`.

The protocol knowledge comes from [Solaar](https://github.com/pwr-Solaar/Solaar)
and [libratbag](https://github.com/libratbag/libratbag), checked against the
real devices. This program tags its HID++ requests with its own software id, so
it can run next to Solaar; just don't let both drive the same device's lighting.

## Limitations

* **G915 X key remapping, G-keys and on-board keyboard profiles**: this
  keyboard has no G-key feature (0x8010) and no 0x8100 memory. It exposes newer
  features (0x1B05 Full Key Customization, 0x8101 Profile Management) that are
  not publicly documented and not implemented by Solaar or libratbag yet.
  Lighting works; key remapping is future work (it needs packet captures of
  G HUB on Windows).
* On-board macros are experimental: the byte format comes from libratbag and
  has not been confirmed on the G502 X yet. Each is limited to one memory sector
  (about 80 key events) and plays once; repeat modes need host mode.
* Mouse wheel up/down remapping in host mode is not handled (the wheel keeps
  scrolling normally).
* Lighting set from the app without a running host-mode profile lasts until
  the keyboard sleeps.
* Keyboards can't report the colours they are showing. When the G915 X runs the
  effects G HUB stored on it, the app shows their names but can't draw them; the
  picture shows the profile instead. Once this app sets the lighting, the
  picture matches the keyboard.

## License

GPL-2.0-or-later, like the projects it learned from.
