from logi_ghub import design, layout, presets
from logi_ghub.features import rgb_store

ZONES = list(range(1, 112)) + [153, 155, 156, 157, 158, 210]


def test_python_romania_design_matches_the_original_animation():
    d = presets.python_romania_design()
    frames, period, data = design.fit_frames(d, ZONES)
    assert period == 48 and len(frames) == 150
    info = rgb_store.parse_file(data)
    assert info["name"] == "Python + Romania" and len(info["frames"]) == 150
    a = layout.zone_of("A")
    assert all(f[a] == presets.PY_STRING for f, _ in frames)  # letters never move
    ins, pgup = layout.zone_of("Insert"), layout.zone_of("PageUp")
    assert len({f[ins] for f, _ in frames}) > 10  # the flag ripples
    assert max(f[ins] for f, _ in frames) == presets.RO_BLUE and max(f[pgup] for f, _ in frames) & 0xFF0000 == 0xFF0000


def test_layers_stack_and_round_trip():
    d = design.Design("t", [design.Layer("all", ["A", "B"], "static", [0x112233]),
                            design.Layer("top", ["B"], "static", [0x445566])])
    f = d.renderer(ZONES).frame(0)
    assert f[layout.zone_of("A")] == 0x112233 and f[layout.zone_of("B")] == 0x445566
    assert f[layout.zone_of("C")] == 0  # in no layer: dark
    assert design.Design.from_dict(d.to_dict()) == d
    assert not d.animated and d.loop_ms() == 0 and len(d.renderer(ZONES).frames(48)) == 1


def test_loop_covers_every_layer():
    d = design.Design("t", [design.Layer("a", ["A"], "breathing", [0xFF0000], period_ms=1500),
                            design.Layer("b", ["B"], "rainbow", period_ms=2000)])
    assert d.loop_ms() == 6000


def test_older_lighting_converts_to_a_design_that_looks_the_same():
    from logi_ghub import lightfx

    a, b = layout.zone_of("A"), layout.zone_of("B")
    light = {"keys": {str(a): 0xFF0000}, "animation": {"enabled": True, "colors": [0x00FF00, 0x0000FF], "step_ms": 500,
                                                        "fade": True, "pattern": "wave_right", "keep_painted": True}}
    d = design.from_lighting(light)
    new, old = d.renderer(ZONES), lightfx.Renderer(light, ZONES)
    for t in (0.0, 0.3, 0.77):
        fn, fo = new.frame(t), old.frame(t)
        assert fn[a] == 0xFF0000
        assert abs((fn[b] >> 8 & 255) - (fo[b] >> 8 & 255)) <= 40  # same wave, positions measured slightly differently
