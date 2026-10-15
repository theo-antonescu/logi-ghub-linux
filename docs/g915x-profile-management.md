# G915 X: Profile Management (0x8101), from a G HUB recording

Recorded with `tools/ghub-capture` (G HUB 2026 in a Windows VM, keyboard on USB).
Function numbers are HID++ function indexes; byte strings are the request parameters.

## Reading (implemented in `features/profile_fs.py`)

| fn | request | reply | meaning |
|---|---|---|---|
| 0 | `00 00 00` | `01 01 00 80 05 80 7f 7f 07 14 ff ff ff 20 03 07` | info (not decoded) |
| 8 | `bank file size_hi size_lo` | zeros | open a file for reading |
| 12 | `off_hi off_lo 00` | 16 bytes | read from the open file |

Bank 0 holds the profiles in use (G HUB names them), bank 1 the factory profiles.
File 0 of a bank is its directory (0x400 bytes): `crc32 count` then
`profile 81 01 40 63 crc32 file` per profile, `ff` at the end. Profile files are 0x63 bytes:
timeouts, a 12-character UTF-16BE name, then the 0x8071 effect (id + 10 parameters) used at
full power and the one used in power-save mode.

Effect 0x0C plays one of the custom animations stored on the keyboard; 0x8071 getInfo with
type 1 returns their names and UUIDs. G HUB never reads the animation frames back.

## Mode and active profile (tested on the keyboard)

* fn 6 `00 00 00` is a query: the reply starts `mode 00 profile`, with mode 1 for on-board and 0 while
  the host owns the keyboard. fn 6 `05` switches to host mode and fn 6 `03` back to on-board; neither
  changes the profile number.
* A slot picked with brightness + N on the keyboard is not part of that state, and no query
  tested reports it. Any lighting takeover (0x8071 software control, with or without host mode) makes the
  keyboard forget it within a few seconds of the hand-back, and it then shows the active profile's
  own lighting.
* 0x8071 fn 5 `01 03 06` claims software control with the host handling power: the keyboard sends
  fn 1 user-activity events (`00` idle after the power-save timeout, `01` active), and the host
  switches the LEDs off and on with fn 8. With flags `04` only, the firmware runs its own power-save
  and puts the stored lighting back over the host's colours.
* While a stored custom animation is the zone's effect, it keeps painting keys over host per-key
  colours. Setting the zone to the fixed effect (not persisted) stops it; the animation may still
  finish its current frame.

## Writing (seen, not implemented)

G HUB also sent these while starting up; their exact meaning is not known yet:

* fn 6 `05 00 00` (host mode, as Solaar uses), fn 6 `00 00 00` / `0f 00 00` (reply `03 00 01`)
* fn 2 `00 02 00`, then fn 3 `00 00 00` (reply `00 01`)
* fn 9 `81 01 01 00 01 00 63 <crc32 of profile 1>`: refers to profile 1's file and its CRC
* fn 9 `1b 05 <profile> 02 00 00 02 41 d9 12 ff`: per profile, a 0x1B05 (key customisation) table
* 0x8071 fn 7 `01 00 00 00 3c 01 0e`: power-save after 60 s, off after 270 s

## Custom animations (implemented in `features/rgb_store.py`)

Recorded while G HUB saved an effect named "Claude Test" to the brightness + 9 slot.
G HUB uploads a file with the DFU feature (0x00D0): fn 4 `04 <slot> "RGB_V0000"` starts it,
a notification says the keyboard is ready, then the file goes out in 16-byte packets on
function `n % 4` (n from 1); each reply echoes n and a status (1 continue, 2 complete).

The file is a 0x4B-byte header (name, UUID, frame count, frame period in ms, brightness)
followed by 17-byte records that are per-key lighting (0x8081) commands as sent live
(`1b`, `5b`, `6b`) and `7b` frame ends with the frame index and duration. It is padded with
0xFF to 12304 bytes and followed by a 16-byte trailer `03 00 .. 00`; without the trailer the
keyboard rejects the upload with status 0x98 and marks the slot invalid. Slot 0 is
brightness + 8, slot 1 brightness + 9. There is no command to read an animation back.
