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

## Writing (seen, not implemented)

G HUB also sent these while starting up; their exact meaning is not known yet:

* fn 6 `05 00 00` (host mode, as Solaar uses), fn 6 `00 00 00` / `0f 00 00` (reply `03 00 01`)
* fn 2 `00 02 00`, then fn 3 `00 00 00` (reply `00 01`)
* fn 9 `81 01 01 00 01 00 63 <crc32 of profile 1>`: refers to profile 1's file and its CRC
* fn 9 `1b 05 <profile> 02 00 00 02 41 d9 12 ff`: per profile, a 0x1B05 (key customisation) table
* 0x8071 fn 7 `01 00 00 00 3c 01 0e`: power-save after 60 s, off after 270 s
