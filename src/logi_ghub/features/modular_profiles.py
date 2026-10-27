"""Read-only Logitech PRO X2 RAPID 0x8102 modular on-board profile tags.

Proven on USB 046d:c364, firmware U179.01_B0011, 27 October 2026.
HID++ LONG (0x11) requests: fn2 active, fn6 tag header, fn7 fragments.
No session (fn4), select (fn3) or flash write/commit methods exist here.

This is a known-tag export, NOT a restorable flash or RGB backup.
Implementation based on on-device verification and HolyJoey/Lghub's 0x8102 analysis.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
from datetime import datetime, timezone
from pathlib import Path

DEVICE_KEY = '046d:c364'
FEATURE = 0x8102
FRAGMENT_SIZE = 15
MAX_TAG_BYTES = 3840  # up to 256 15-byte fragments; no seq wrap permitted
MAX_FRAGMENT_CALLS_TOTAL = 2000

# Known or documented 0x8102 profile tag IDs. Unknown formats are preserved as
# raw hex with hash/length, NOT speculatively decoded.
TAGS = {
    0x0003: 'profile_name',
    0x0004: 'key_mapping',
    0x0100: 'wired_report_rate',
    0x0101: 'wireless_report_rate',
    0x0102: 'game_mode_disabled_keys',
    0x0103: 'immersive_lighting_enabled',
    0x0104: 'immersive_lighting_active_cluster_0',
    0x0105: 'immersive_lighting_active_cluster_1',
    0x0106: 'immersive_lighting_passive_cluster_0',
    0x0107: 'immersive_lighting_passive_cluster_1',
    0x0110: 'base_layer_assignments',
    0x0111: 'fn_layer_assignments',
    0x0112: 'gshift_layer_assignments',
    0x0113: 'macros',
    0x0120: 'global_actuation',
    0x0121: 'rapid_trigger_enabled',
    0x0122: 'key_priority_enabled',
    0x0123: 'per_key_actuation',
    0x0124: 'per_key_rapid_trigger',
    0x0130: 'key_priority_pairs',
    0xFF01: 'profile_checksum',
}


class CallBudget:
    def __init__(self):
        self.fragments = 0
        self.requests = 0

    def call(self, hidpp, fn: int, params: bytes = b'') -> bytes:
        # Safeguard: only known read operations may be sent to firmware.
        if fn not in (2, 6, 7):
            raise ValueError(f'FORBIDDEN HID++ 0x8102 fn={fn}')
        if fn == 2 and params:
            raise ValueError('fn2 must have no arguments')
        if fn == 6 and (len(params) != 3 or params[0] not in (1, 2, 3)
                        or int.from_bytes(params[1:3], 'big') not in TAGS):
            raise ValueError('fn6 profile/tag outside allowlist')
        if fn == 7:
            if len(params) != 1:
                raise ValueError('fn7 requires exactly one sequence byte')
            if self.fragments >= MAX_FRAGMENT_CALLS_TOTAL:
                raise ValueError('global safety limit for fragment calls reached')
            self.fragments += 1
        self.requests += 1
        return hidpp.call(FEATURE, fn, params) if params else hidpp.call(FEATURE, fn)


def decode(tag: int, blob: bytes) -> dict:
    n = len(blob)
    if tag == 0x0003:
        if n != 64:
            raise ValueError(f'profile name length {n} != 64')
        if all(b == 0xff for b in blob):
            return {'name': ''}
        return {'name': blob.decode('utf-16-be', errors='replace').split('\x00')[0]}
    if tag in (0x0100, 0x0101):
        if n != 1:
            raise ValueError(f'report rate length {n} != 1')
        code = blob[0]
        return {'code': code, 'configured_hz': (125 << code) if code <= 6 else None}
    if tag in (0x0103, 0x0121, 0x0122):
        if n != 1:
            raise ValueError(f'flag length {n} != 1')
        return {'code': blob[0], 'enabled': bool(blob[0]) if blob[0] in (0, 1) else None}
    if tag == 0x0120:
        if n != 2:
            raise ValueError(f'actuation length {n} != 2')
        return {'mm': struct.unpack('>H', blob)[0] / 100}
    if tag in (0x0123, 0x0124):
        if n % 3:
            raise ValueError(f'per-key tag length {n} not divisible by 3')
        entries = [
            {'control_index': blob[i], 'mm': struct.unpack('>H', blob[i+1:i+3])[0] / 100}
            for i in range(0, n, 3)
        ]
        indexes = [entry['control_index'] for entry in entries]
        return {'count': len(entries), 'unique_control_indices': len(set(indexes)) == len(indexes), 'entries': entries}
    if tag == 0x0130:
        if n % 4:
            raise ValueError(f'priority pair tag length {n} not divisible by 4')
        return {'count': n // 4, 'pairs': [
            {'a': blob[i], 'b': blob[i+1], 'mode_code': blob[i+2],
             'flags': blob[i+3], 'raw_hex': blob[i:i+4].hex(' ')}
            for i in range(0, n, 4)
        ]}
    if tag in (0x0104, 0x0105, 0x0106, 0x0107) and n == 11:
        # Confirmed bytes differ across P1/P2/P3. Exact effects semantics and
        # relation to keyboard's current lighting are not yet verified.
        return {'effect_code': blob[0],
                'rgb_candidate': f'#{blob[1]:02x}{blob[2]:02x}{blob[3]:02x}' if blob[0] == 1 else None,
                'verification': 'candidate_only'}
    if tag == 0x0102 and n == 16:
        return {'disabled_control_indices': [i for i in range(128) if blob[i // 8] & (1 << (i % 8))]}
    # For other tags, the exact format is not verified for this firmware.
    return {'decode_status': 'raw_only', 'note': 'format not confirmed for this keyboard'}


def read_tag(hidpp, budget: CallBudget, profile: int, tag: int) -> dict:
    hdr = budget.call(hidpp, 6, bytes([profile]) + tag.to_bytes(2, 'big'))
    if len(hdr) < 3:
        raise ValueError(f'fn6 header too short: {len(hdr)}')
    base_seq = hdr[0]
    declared = int.from_bytes(hdr[1:3], 'big')
    if declared > MAX_TAG_BYTES:
        raise ValueError(f'tag declared {declared} bytes, above safety limit {MAX_TAG_BYTES}')
    fragments = math.ceil(declared / FRAGMENT_SIZE)
    if base_seq + fragments > 256:
        raise ValueError(f'sequence would wrap (start {base_seq}, fragments {fragments})')
    out = bytearray()
    for index in range(fragments):
        seq = base_seq + index
        reply = budget.call(hidpp, 7, bytes([seq]))
        wanted = min(FRAGMENT_SIZE, declared - len(out))
        if len(reply) < wanted + 1:
            raise ValueError(f'fragment seq={seq}: response too short')
        if reply[0] != seq:
            raise ValueError(f'fragment seq mismatch: expected {seq}, got {reply[0]}')
        out.extend(reply[1:1+wanted])
    if len(out) != declared:
        raise ValueError(f'incomplete: got {len(out)}, expected {declared}')
    raw = bytes(out)
    result = {
        'tag': f'0x{tag:04x}', 'declared_length': declared, 'actual_length': len(raw),
        'fragment_count': fragments, 'base_sequence': base_seq, 'complete': True,
        'raw_hex': raw.hex(' '), 'sha256': hashlib.sha256(raw).hexdigest(),
    }
    try:
        result['decoded'] = decode(tag, raw)
    except ValueError as exc:
        result['decode_warning'] = str(exc)
    return result


def concise(entry: dict, label: str) -> str:
    if 'error' in entry:
        return 'ERROR ' + entry['error']
    d = entry.get('decoded', {})
    if label == 'profile_name':
        return 'name=' + repr(d.get('name'))
    if 'configured_hz' in d:
        return f"{d['configured_hz']} Hz (stored, not measured)"
    if 'mm' in d:
        return f"{d['mm']:.2f} mm"
    if 'enabled' in d:
        return f"enabled={d['enabled']}"
    if 'count' in d:
        return f"{d['count']} entries"
    return f"{entry['actual_length']} B"


def compare_profiles(report: dict) -> dict:
    p = report['profiles']
    result = {}
    for tag, name in TAGS.items():
        rows = [p[str(i)].get(name, {}) for i in (1, 2, 3)]
        if any(not row.get('complete') for row in rows):
            status = 'unavailable_or_failed'
        elif len({row['sha256'] for row in rows}) == 1:
            status = 'identical'
        else:
            status = 'different'
        result[name] = status
    return result



def save_private_json(path: Path, payload: dict) -> None:
    """Owner-only JSON, to protect user-defined macros/assignments."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump(payload, out, indent=2, ensure_ascii=False)
        out.write('\n')


def sanitized_summary(report: dict) -> dict:
    """Summary without raw profile tags, macro contents or assignments."""
    useful = {
        'profile_name', 'wired_report_rate', 'wireless_report_rate',
        'global_actuation', 'rapid_trigger_enabled', 'key_priority_enabled',
        'immersive_lighting_enabled', 'per_key_actuation',
        'per_key_rapid_trigger', 'key_priority_pairs',
    }
    out = {
        'device': report['device'], 'captured_utc': report['captured_utc'],
        'active_before': report.get('active_before'),
        'active_after': report.get('active_after'),
        'comparison': report.get('comparison', {}),
        'statistics': report.get('statistics', {}),
        'profiles': {},
    }
    for profile, tags in report.get('profiles', {}).items():
        result = out['profiles'][profile] = {}
        for label, val in tags.items():
            info = {'tag': val['tag'], 'status': val.get('status', 'unknown')}
            if 'error' in val:
                info['error'] = val['error']
            else:
                info['length'] = val.get('actual_length')
                info['sha256'] = val.get('sha256')
                if label in useful:
                    decoded = val.get('decoded', {})
                    if label == 'profile_name':
                        info['name'] = decoded.get('name')
                    elif label in ('per_key_rapid_trigger', 'per_key_actuation'):
                        info['count'] = decoded.get('count')
                        info['unique_control_indices'] = decoded.get('unique_control_indices')
                    elif label == 'key_priority_pairs':
                        info['count'] = decoded.get('count')
                    else:
                        info['decoded'] = decoded
                if 'decode_warning' in val:
                    info['decode_warning'] = val['decode_warning']
            result[label] = info
    return out




def export_profiles(dev, slots=(1, 2, 3)) -> dict:
    """Read verified on-board tags without selecting profiles or writing memory.

    ``dev`` is an opened LogiDevice. The HID++ report mode is restored even
    when a request fails. Caller is responsible for closing the device.
    """
    if dev.key.lower() != DEVICE_KEY or dev.hidpp is None or not dev.has(FEATURE):
        raise ValueError('PRO X2 RAPID (046d:c364) with HID++ 0x8102 is required')
    slots = tuple(slots)
    if not slots or any(slot not in (1, 2, 3) for slot in slots):
        raise ValueError('slot must be 1, 2 or 3')
    report = {
        'device': DEVICE_KEY,
        'captured_utc': datetime.now(timezone.utc).isoformat(),
        'protocol': 'HID++ 0x8102, LONG 0x11; fn2/fn6/fn7 only',
        'read_only': True,
        'scope': 'known tags only; NOT a restorable flash/RGB backup',
        'profiles': {},
    }
    hidpp = dev.hidpp
    previous_long_only = hidpp.long_only
    budget = CallBudget()
    try:
        hidpp.long_only = True
        before = budget.call(hidpp, 2)
        if not before or before[0] not in (1, 2, 3):
            raise ValueError(f'unexpected active profile response: {before.hex(" ")}')
        report['active_before'] = before[0]
        for slot in slots:
            one = report['profiles'][str(slot)] = {}
            for tag, label in TAGS.items():
                try:
                    row = read_tag(hidpp, budget, slot, tag)
                    row['status'] = 'ok'
                except Exception as exc:  # collect individual unsupported tags; no write fallback
                    # Individual unsupported tags cannot interrupt a complete
                    # device survey; preserve exact error for later analysis.
                    row = {'tag': f'0x{tag:04x}', 'status': 'error',
                           'complete': False, 'error': str(exc)}
                one[label] = row
        after = budget.call(hidpp, 2)
        report['active_after'] = after[0] if after else None
        if report['active_after'] != report['active_before']:
            raise RuntimeError('active profile changed during export; results may be inconsistent')
        report['comparison'] = compare_profiles(report) if len(slots) == 3 else {}
        report['statistics'] = {
            'hidpp_calls': budget.requests,
            'fragment_reads': budget.fragments,
            'tags_attempted': len(slots) * len(TAGS),
            'tag_failures': sum(v['status'] != 'ok'
                                for row in report['profiles'].values() for v in row.values()),
        }
        return report
    finally:
        hidpp.long_only = previous_long_only


def profile_overview(report: dict) -> list[dict]:
    """Compact privacy-conscious overview suitable for terminal output."""
    out = []
    for slot, tags in report['profiles'].items():
        def decoded(tag):
            return tags[tag].get('decoded', {})
        lighting = decoded('immersive_lighting_active_cluster_1')
        out.append({
            'slot': int(slot),
            'name': decoded('profile_name').get('name'),
            'wired_report_rate_hz': decoded('wired_report_rate').get('configured_hz'),
            'global_actuation_mm': decoded('global_actuation').get('mm'),
            'rapid_trigger': decoded('rapid_trigger_enabled').get('enabled'),
            'key_priority': decoded('key_priority_enabled').get('enabled'),
            'key_priority_pairs': decoded('key_priority_pairs').get('count'),
            'lighting_rgb_candidate': lighting.get('rgb_candidate'),
        })
    return out
