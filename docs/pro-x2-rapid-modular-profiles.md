# PRO X2 RAPID: read-only hardware profiles (HID++ 0x8102)

Support status: **read-only and hardware-verified** on USB VID:PID `046d:c364`
(firmware `U179.01_B0011`, October 2026). This path is distinct from the
legacy HID++ `0x8100` mouse profile memory and G915 X `0x8101` filesystem.

## CLI

With other HID++ programs/daemons stopped, run:

```bash
python3 -m logi_ghub.cli -d 046d:c364 onboard modular
python3 -m logi_ghub.cli -d 046d:c364 onboard modular --slot 2
python3 -m logi_ghub.cli -d 046d:c364 onboard modular --out ~/.local/state/pro-x2-rapid/export.json
```

The first two commands print **concise, redacted configuration summaries**.
`--out` additionally writes the complete raw known-tag export to an owner-only
JSON file and a second, redacted `_summary.json` file. Raw tag exports may
include macro and key-assignment data; do not publish without inspection.
Neither output is a *restorable backup*, nor a dump of the full lighting memory.

## Verified protocol

Requests use **HID++ LONG report `0x11`** on feature `0x8102`:

| Function | Parameters | Result |
| --- | --- | --- |
| 2 | none | active hardware profile (1, 2, 3) |
| 6 | `[slot, tag_hi, tag_lo]` | `[base_seq, size_hi, size_lo, ...]` |
| 7 | `[base_seq + offset]` | `[seq, 15 data bytes]` |

Read individual tags independently; **no session function** `fn4` is needed.
The firmware rejects an attempted `fn4 END` in prior diagnostics. Software
must never issue it as a cleanup step for read-only operations.

The read-only implementation has a strict allowlist of `fn2`, `fn6` and `fn7`,
verifies fragment sequence numbers, checks declared lengths (at most 3840
bytes), rejects sequence wrapping and restores the HID++ report setting after
export.

## Confirmed tags

- `0x0003`: full 64-byte UTF-16BE name
- `0x0100`: stored USB report-rate code (`125 << code` Hz)
- `0x0102`: game mode disabled-control bitmap
- `0x0103..0x0107`: immersive lighting configuration; 11-byte effect records
  are kept raw (RGB interpretation of the static-effect bytes is tentative)
- `0x0110..0x0113`: assignment layers and macro data (stored raw)
- `0x0120`: global actuation point, millimetres × 100, u16 BE
- `0x0121`: rapid trigger enabled (byte)
- `0x0122`: key priority enabled (byte)
- `0x0123`: per-control actuation overrides, each `[control_index, u16 BE]`
- `0x0124`: per-control rapid-trigger sensitivity, likewise; verified
  384 bytes = 26 fragments = 128 entries
- `0x0130`: key priority pairs, groups of four bytes
- `0xff01`: firmware may return an empty tag; *not* a validated checksum

`0x0004` (key mappings) and `0x0101` (wireless report rate) both returned
`invalid argument` on this wired keyboard. Their failures are recorded but
are not treated as failures of the successful tag reads.

The physical `Fn+1` / `Fn+2` / `Fn+3` shortcuts generate unsolicited
`0x8102 fn1` notifications with profile number as the first byte.

## Out of scope

No writes (`fn9`, `fn10`, `fn11`), no selection (`fn3`), no session (`fn4`),
no unknown engineering features. The correct destination for writes remains
unverified, so flash writes cannot safely be supported yet. `0x8071` RGB and
other onboard lighting/animation state may use separate storage; this exporter
does not promise a complete lighting backup or restore.

Protocol background: https://github.com/HolyJoey/Lghub/blob/main/device_settings.py
