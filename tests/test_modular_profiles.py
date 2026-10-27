"""PRO X2 RAPID 0x8102 read-only protocol regression tests."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from logi_ghub.features.modular_profiles import (
    TAGS, CallBudget, decode, export_profiles, profile_overview,
    read_tag, sanitized_summary, save_private_json,
)


class FakeProtocol:
    long_only = False

    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.calls = []
        self.selected = b''

    def call(self, feature, fn, params=b''):
        assert feature == 0x8102
        assert self.long_only
        assert fn in (2, 6, 7), 'unexpected mutating/session HID++ function'
        self.calls.append((feature, fn, params))
        if fn == 2:
            return bytes([2]) + bytes(15)
        if fn == 6:
            key = (params[0], int.from_bytes(params[1:], 'big'))
            if key[1] in (0x0004, 0x0101):
                raise ValueError('HID++ invalid argument')
            self.selected = self.mapping.get(key, b'')
            return b'\0' + len(self.selected).to_bytes(2, 'big') + bytes(13)
        seq = params[0]
        return bytes([seq]) + self.selected[seq * 15:(seq + 1) * 15].ljust(15, b'\0')


class FakeDevice:
    key = '046d:c364'

    def __init__(self, mapping):
        self.hidpp = FakeProtocol(mapping)

    def has(self, feature):
        return feature == 0x8102


def fixture_mapping():
    mapping = {}
    for slot in (1, 2, 3):
        mapping[slot, 0x0003] = f'P{slot}'.encode('utf-16-be').ljust(64, b'\0')
        mapping[slot, 0x0100] = bytes([6])
        mapping[slot, 0x0102] = bytes([0, 0, 0, 0, 0, 0x42]) + bytes(10)
        mapping[slot, 0x0103] = bytes([0])
        rgb = {1: (0, 200, 255), 2: (255, 114, 3), 3: (0, 0, 255)}[slot]
        for tag in (0x0105, 0x0107):
            mapping[slot, tag] = bytes([1, *rgb]) + bytes(7)
        mapping[slot, 0x0120] = struct.pack('>H', 200 if slot == 1 else 100)
        mapping[slot, 0x0121] = b'\0'
        mapping[slot, 0x0122] = b'\0'
        mapping[slot, 0x0124] = b''.join(bytes([n, 0, 50]) for n in range(128))
        mapping[slot, 0x0130] = bytes([0x19, 0x1B, 1, 0]) if slot == 2 else b''
    return mapping


def test_pro_x2_full_read_three_profiles_and_differences():
    dev = FakeDevice(fixture_mapping())
    r = export_profiles(dev)
    assert r['active_before'] == r['active_after'] == 2
    assert dev.hidpp.long_only is False
    assert r['statistics']['tag_failures'] == 6
    assert r['statistics']['tags_attempted'] == len(TAGS) * 3
    assert r['profiles']['2']['profile_name']['decoded']['name'] == 'P2'
    assert r['profiles']['1']['global_actuation']['decoded']['mm'] == 2.0
    assert r['profiles']['2']['global_actuation']['decoded']['mm'] == 1.0
    assert r['comparison']['immersive_lighting_active_cluster_1'] == 'different'
    assert r['comparison']['per_key_rapid_trigger'] == 'identical'
    assert r['profiles']['3']['per_key_rapid_trigger']['fragment_count'] == 26
    assert len(r['profiles']['3']['per_key_rapid_trigger']['decoded']['entries']) == 128
    assert r['profiles']['2']['key_priority_pairs']['decoded']['pairs'] == [
        {'a': 25, 'b': 27, 'mode_code': 1, 'flags': 0, 'raw_hex': '19 1b 01 00'}
    ]
    assert all(fn in (2, 6, 7) for _, fn, _ in dev.hidpp.calls)
    assert len(dev.hidpp.calls) == r['statistics']['hidpp_calls']
    assert len(dev.hidpp.calls) >= 150
    assert profile_overview(r)[0]['lighting_rgb_candidate'] == '#00c8ff'


def test_single_profile_reader():
    dev = FakeDevice(fixture_mapping())
    r = export_profiles(dev, (2,))
    assert set(r['profiles']) == {'2'}
    assert r['comparison'] == {}
    assert len(dev.hidpp.calls) == 64


@pytest.mark.parametrize('fn', [0, 1, 3, 4, 5, 8, 9, 10, 11])
def test_write_select_or_session_are_forbidden(fn):
    p = FakeProtocol()
    with pytest.raises(ValueError, match='FORBIDDEN'):
        CallBudget().call(p, fn)
    assert p.calls == []


def test_unsupported_device_rejected():
    dev = FakeDevice({})
    dev.key = '046d:c35b'
    with pytest.raises(ValueError, match='PRO X2 RAPID'):
        export_profiles(dev)


def test_fragment_sequence_mismatch_and_restore_long():
    class BadProtocol(FakeProtocol):
        def call(self, feature, fn, params=b''):
            r = super().call(feature, fn, params)
            if fn == 7:
                return bytes([(r[0] + 1) & 255]) + r[1:]
            return r

    dev = FakeDevice(fixture_mapping())
    dev.hidpp = BadProtocol(fixture_mapping())
    r = export_profiles(dev, (1,))
    assert dev.hidpp.long_only is False
    assert r['profiles']['1']['profile_name']['status'] == 'error'
    assert 'seq mismatch' in r['profiles']['1']['profile_name']['error']


def test_only_private_raw_export(tmp_path):
    dev = FakeDevice(fixture_mapping())
    r = export_profiles(dev, (2,))
    target = tmp_path / 'profiles.json'
    save_private_json(target, r)
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text())['profiles']['2']['profile_name']['status'] == 'ok'
    sanitized = sanitized_summary(r)
    assert 'raw_hex' not in json.dumps(sanitized)
    assert 'entries' not in json.dumps(sanitized)
    assert sanitized['profiles']['2']['profile_name']['name'] == 'P2'


def test_oversize_raises_before_fragment_calls():
    class Oversize(FakeProtocol):
        def call(self, feature, fn, params=b''):
            if fn == 6:
                return bytes([0, 0xFF, 0xFF]) + bytes(13)
            return super().call(feature, fn, params)
    p = Oversize()
    p.long_only = True
    with pytest.raises(ValueError, match='safety limit'):
        read_tag(p, CallBudget(), 1, 0x0124)
    assert not any(fn == 7 for _, fn, _ in p.calls)


def test_documented_decoders():
    assert decode(0x0102, bytes([0, 0, 0, 0, 0, 0x42])+bytes(10))['disabled_control_indices'] == [41, 46]
    assert decode(0x0120, bytes([0, 200]))['mm'] == 2.0
    assert decode(0x0124, bytes([3, 0, 50]))['entries'] == [{'control_index': 3, 'mm': 0.5}]
