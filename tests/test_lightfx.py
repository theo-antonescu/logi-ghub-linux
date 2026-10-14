from logi_ghub import lightfx
from logi_ghub.device import LogiDevice
from logi_ghub.features import lighting
from logi_ghub.hidpp.fake import fake_devices

W = lighting.zone_for_hid(0x1A)  # W key
A = lighting.zone_for_hid(0x04)
ESC = lighting.zone_for_hid(0x29)
ENTER = lighting.zone_for_hid(0x28)


def test_sequence_color_steps_and_fades():
    seq = [0xFF0000, 0x00FF00, 0x0000FF]
    assert lightfx.sequence_color(seq, 0.0, False) == 0xFF0000
    assert lightfx.sequence_color(seq, 1.2, False) == 0x00FF00
    assert lightfx.sequence_color(seq, 3.0, False) == 0xFF0000  # loops
    assert lightfx.sequence_color(seq, 0.5, True) == 0x808000


def test_static_with_painted_keys():
    r = lightfx.Renderer({"zones": {"0": {"effect": 1, "color": 0x0000FF}}, "keys": {str(W): 0xFF0000}})
    f = r.frame(1.0)
    assert f[W] == 0xFF0000 and f[A] == 0x0000FF
    assert not r.is_animated()


def test_animation_all_keys_follow_sequence_but_painted_stay():
    light = {"keys": {str(W): 0xFFFFFF},
             "animation": {"enabled": True, "colors": [0xFF0000, 0x00FF00], "step_ms": 1000, "fade": False,
                           "pattern": "all", "keep_painted": True}}
    r = lightfx.Renderer(light)
    assert r.is_animated()
    assert r.frame(0.1)[A] == 0xFF0000 and r.frame(1.1)[A] == 0x00FF00
    assert r.frame(1.1)[W] == 0xFFFFFF


def test_wave_moves_across_the_keyboard():
    light = {"animation": {"enabled": True, "colors": [0xFF0000, 0x00FF00, 0x0000FF], "step_ms": 1000, "fade": False,
                           "pattern": "wave_right", "keep_painted": False}}
    r = lightfx.Renderer(light)
    f = r.frame(0.0)
    assert f[ESC] != f[ENTER]  # left and right of the board are at different points of the sequence


def test_apply_animation_drives_per_key_frames():
    kb: LogiDevice = fake_devices()[1]
    light = {"animation": {"enabled": True, "colors": [0x112233], "step_ms": 1000, "fade": False, "pattern": "all"}}
    anim = lightfx.apply(kb, light)
    import time

    time.sleep(0.3)
    anim.stop()
    anim.join(1)
    m = kb.fake_model
    assert m.sw_control == (3, 6)  # software control with host power handling
    assert m.keys and set(m.keys.values()) == {0x112233}
