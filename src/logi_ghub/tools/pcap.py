"""Decode HID++ traffic from a usbmon pcap (e.g. one written by tools/ghub-capture/ghub-vm.sh).

    python3 -m logi_ghub.tools.pcap capture.pcap [--feature 0x8101] [--summary]

Feature indexes are learned from the capture itself (Root.getFeature and
FeatureSet.getFeatureId replies), so feature names are shown when G HUB looked them up.
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter
from dataclasses import dataclass

from ..hidpp import features as F

LINKTYPE_USB_LINUX = 189  # 48-byte usbmon header
LINKTYPE_USB_LINUX_MMAPPED = 220  # 64-byte usbmon header
HIDPP_SIZES = {0x10: 7, 0x11: 20, 0x12: 64}


@dataclass
class Packet:
    time: float
    out: bool  # host -> device
    data: bytes


def read_usb_payloads(path: str):
    with open(path, "rb") as f:
        header = f.read(24)
        magic = header[:4]
        endian = "<" if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
        nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
        linktype = struct.unpack(endian + "I", header[20:24])[0]
        hdr_len = 64 if linktype == LINKTYPE_USB_LINUX_MMAPPED else 48
        while True:
            rec = f.read(16)
            if len(rec) < 16:
                return
            ts_sec, ts_frac, incl, _orig = struct.unpack(endian + "IIII", rec)
            body = f.read(incl)
            if len(body) < hdr_len:
                continue
            urb_type = chr(body[8])
            xfer_type = body[9]
            epnum = body[10]
            setup_flag = body[14]
            setup = body[40:48]
            data = body[hdr_len:]
            t = ts_sec + ts_frac / (1e9 if nano else 1e6)
            if xfer_type == 2 and urb_type == "S" and setup_flag == 0 and setup[0] == 0x21 and setup[1] == 0x09:
                yield Packet(t, True, data)  # SET_REPORT carrying an output report
            elif xfer_type == 1 and urb_type == "S" and not epnum & 0x80 and data:
                yield Packet(t, True, data)  # interrupt OUT
            elif xfer_type == 1 and urb_type == "C" and epnum & 0x80 and data:
                yield Packet(t, False, data)  # interrupt IN


def hidpp_messages(path: str):
    for p in read_usb_payloads(path):
        d = p.data
        if d and d[0] in HIDPP_SIZES and len(d) >= HIDPP_SIZES[d[0]]:
            yield p.time, p.out, d[: HIDPP_SIZES[d[0]]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="logi_ghub.tools.pcap")
    ap.add_argument("pcap")
    ap.add_argument("--feature", help="only show this feature id, e.g. 0x8101")
    ap.add_argument("--summary", action="store_true", help="count calls per feature and function")
    args = ap.parse_args(argv)
    index_to_feature: dict[int, int] = {0: F.ROOT}
    pending_root: dict[int, int] = {}
    featureset_index = None
    want = int(args.feature, 16) if args.feature else None
    counts: Counter = Counter()
    t0 = None
    for t, out, m in hidpp_messages(args.pcap):
        t0 = t0 if t0 is not None else t
        sub, addr, params = m[2], m[3], m[4:]
        fn, swid = addr >> 4, addr & 0x0F
        # learn feature indexes
        if sub == 0 and fn == 0:
            if out:
                pending_root[swid] = struct.unpack(">H", params[0:2])[0]
            elif swid in pending_root and params[0]:
                index_to_feature[params[0]] = pending_root.pop(swid)
                if index_to_feature[params[0]] == F.FEATURE_SET:
                    featureset_index = params[0]
        if featureset_index is not None and sub == featureset_index and fn == 1:
            if out:
                pending_root[("fs", swid)] = params[0]
            elif ("fs", swid) in pending_root:
                index_to_feature[pending_root.pop(("fs", swid))] = struct.unpack(">H", params[0:2])[0]
        fid = index_to_feature.get(sub)
        if sub in (0x8F, 0xFF):
            label = "ERROR"
        else:
            label = F.short_name(fid) if fid is not None else f"index {sub}"
        if want is not None and fid != want:
            continue
        counts[(label, fn, "out" if out else "in")] += 1
        if not args.summary:
            arrow = "->" if out else "<-"
            note = " (notification)" if not out and swid == 0 else ""
            print(f"{t - t0:9.3f} {arrow} {label:24s} fn{fn:<2d} sw{swid:x} {params.hex(' ')}{note}")
    if args.summary:
        for (label, fn, d), n in sorted(counts.items()):
            print(f"{label:24s} fn{fn:<2d} {d:3s} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
