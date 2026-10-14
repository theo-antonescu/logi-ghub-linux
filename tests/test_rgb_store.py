import pytest

from logi_ghub.features import rgb_store as rs

ZONES = list(range(1, 112)) + [153, 155, 156, 157, 158, 210]


def test_file_layout_and_round_trip():
    frames = [({z: 0xFF0000 if z % 2 else 0x00FF00 for z in ZONES}, 1), ({z: 0x0000FF for z in ZONES}, 16)]
    data = rs.build_file("Test", frames, uid=bytes(range(16)))
    assert len(data) == rs.FILE_SIZE and data[-16:] == rs.TRAILER
    assert data[0] == 1 and data[7] == 0x30 and data[0x10:0x14] == b"Test" and data[0x30:0x40] == bytes(range(16))
    info = rs.parse_file(data)
    assert info["name"] == "Test" and info["frame_count"] == 2 and info["period_ms"] == 32
    assert [f for f, _ in info["frames"]] == [f for f, _ in frames] and [d for _, d in info["frames"]] == [1, 16]


def test_colour_sequence_fits_and_steps():
    light = {"animation": {"enabled": True, "colors": [0xFF0000, 0x00FF00, 0x0000FF], "step_ms": 500, "fade": False,
                           "pattern": "all", "keep_painted": False}}
    frames = rs.frames_from_lighting(light, ZONES)
    assert [set(f.values()) for f, _ in frames] == [{0xFF0000}, {0x00FF00}, {0x0000FF}]
    assert sum(d for _, d in frames) * 32 == pytest.approx(1500, abs=40)


def test_fading_wave_is_thinned_to_fit():
    light = {"animation": {"enabled": True, "colors": [0xFF0000, 0x00FF00, 0x0000FF, 0xFFFFFF], "step_ms": 2000,
                           "fade": True, "pattern": "wave_right", "keep_painted": False}}
    frames = rs.frames_from_lighting(light, ZONES)
    rs.build_file("wave", frames)  # does not raise


def test_slot_zero_is_refused():
    class Dev:
        def get_feature_index(self, fid):
            return 18

    with pytest.raises(ValueError):
        rs.Uploader(Dev()).upload(0, rs.build_file("x", [({1: 1}, 1)]))


def test_nothing_to_save():
    with pytest.raises(ValueError):
        rs.frames_from_lighting({}, ZONES)


def test_python_romania_preset_fits():
    from logi_ghub import layout, presets

    frames = presets.python_romania(ZONES)
    data = rs.build_file("Python + Romania", frames)
    info = rs.parse_file(data)
    assert len(info["frames"]) == 150
    f0 = info["frames"][0][0]
    assert f0[layout.zone_of("A")] == presets.PY_STRING and f0[layout.zone_of("Space")] == presets.PY_SPECIAL
    ins = [f[layout.zone_of("Insert")] for f, _ in info["frames"]]
    assert presets.RO_BLUE in ins and len(set(ins)) > 3  # the flag wave moves
    logo = {f[layout.zone_of("Logo")] for f, _ in info["frames"]}
    assert len(logo) > 50  # rainbow
