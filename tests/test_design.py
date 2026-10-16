from logi_ghub import design, layout, presets
from logi_ghub.features import rgb_store

ZONES = list(range(1, 112)) + [153, 155, 156, 157, 158, 210]


def test_python_romania_design_fits_with_a_seamless_loop():
    d = presets.python_romania_design()
    frames, period, data = design.fit_frames(d, ZONES)
    assert sum(n for _, n in frames) * period == d.loop_ms()  # no jump where the loop restarts
    info = rgb_store.parse_file(data)
    assert info["name"] == "Python + Romania" and info["period_ms"] == period
    a = layout.zone_of("A")
    assert all(f[a] == presets.PY_STRING for f, _ in frames)  # letters never move
    ins, pgup = layout.zone_of("Insert"), layout.zone_of("PageUp")
    seen_ins = {f[ins] for f, _ in frames}
    assert len(seen_ins) > 10  # the flag rises, waves and fades
    assert frames[0][0][ins] & 0xFF > 0x80 and frames[0][0][ins] >> 16 > 0x40  # starts as pale sky, not dark
    assert any(c & 0xFF > 0xC0 and c >> 8 & 0xFF < 0x40 for c in seen_ins)  # then flag blue
    assert any(f[pgup] >> 16 > 0xC0 and f[pgup] & 0xFF < 0x40 for f, _ in frames)  # and red on the right


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


def test_flag_effects_use_the_flag_colours_and_loop():
    keys = [k for col in presets.NAV_COLUMNS for k in col]
    flag = [presets.RO_BLUE, presets.RO_YELLOW, presets.RO_RED]
    for effect in ("pinwheel", "comet", "turning", "sparkle", "heartbeat", "raise"):
        layer = design.Layer("f", keys, effect, flag, period_ms=3000, direction="right", floor=0.4, spread=0.4)
        r = design.Design("t", [layer]).renderer(ZONES)
        zones = [layout.zone_of(k) for k in keys]
        seen = {r.frame(t / 10)[z] for t in range(30) for z in zones}
        assert len(seen) > 2, effect  # it moves
        assert all(r.frame(0.7)[z] == r.frame(3.7)[z] for z in zones), effect  # same every loop
    pin = design.Design("t", [design.Layer("f", keys, "pinwheel", flag, 3000)]).renderer(ZONES)
    assert set(pin.frame(0.4).values()) - {0} <= set(flag)


def test_frames_play_in_order_with_their_transitions():
    frames = [{"colors": {"A": 0xFF0000}, "ms": 400, "transition": "linear"},
              {"colors": {"A": 0x0000FF, "B": 0x00FF00}, "ms": 400, "transition": "cut"}]
    d = design.Design("t", [design.Layer("f", [], "frames", frames=frames)])
    r = d.renderer(ZONES)
    a, b = layout.zone_of("A"), layout.zone_of("B")
    assert r.frame(0.0)[a] == 0xFF0000 and r.frame(0.0)[b] == 0
    assert r.frame(0.2)[a] == design.lerp(0xFF0000, 0x0000FF, 0.5)  # half way, linear
    assert r.frame(0.6)[a] == 0x0000FF  # cut: holds until the next frame
    assert d.loop_ms() == 800 and design.Design.from_dict(d.to_dict()) == d
    bounce = design.Layer("f", [], "frames", frames=frames * 2, cycle="bounce")
    assert [c for c, _ms, _t in bounce.timeline()][4] == frames[0]["colors"]  # 1 2 3 4 3 2


def test_rising_flags_alternate_by_row_and_move_up():
    keys = [k for col in presets.NAV_COLUMNS for k in col]
    layer = design.Layer("s", keys, "flag_stream", [], period_ms=500, fade=False, flags=["romania", "canada"])
    r = design.Design("t", [layer]).renderer(ZONES)
    z = layout.zone_of
    f0 = r.frame(0.0)
    assert [f0[z(k)] for k in ("PrintScreen", "ScrollLock", "Pause")] == list(design.FLAG_ROWS["romania"])
    assert [f0[z(k)] for k in ("Insert", "Home", "PageUp")] == list(design.FLAG_ROWS["canada"])
    f1 = r.frame(0.5)  # one step later each row shows the flag that was below it
    assert [f1[z(k)] for k in ("PrintScreen", "ScrollLock", "Pause")] == list(design.FLAG_ROWS["canada"])


def test_raising_flags_alternate_countries():
    keys = [k for col in presets.NAV_COLUMNS for k in col]
    layer = design.Layer("r", keys, "raise", [], period_ms=6000, direction="right", floor=0.45, background=0x50B0FF,
                         flags=["romania", "canada"])
    r = design.Design("t", [layer]).renderer(ZONES)
    z = layout.zone_of
    ro, ca = r.frame(3.0), r.frame(9.0)  # mid-wave of the first and the second flag
    assert ro[z("Insert")] & 0xFF > 0x80 and ro[z("PageUp")] >> 16 > 0x80  # blue left, red right
    assert ca[z("Insert")] >> 16 > 0x80 and ca[z("Home")] >> 16 > 0x80 and ca[z("Home")] & 0xFF < 0x40  # leaf red
    assert ca[z("ScrollLock")] & 0xFF > 0x60  # white middle column
    assert layer.cycle_ms == 12000  # 6 s per flag here


def test_ceremony_raises_flutters_lowers_and_ends_in_fireworks():
    keys = [k for col in presets.NAV_COLUMNS for k in col]
    flag = [presets.RO_BLUE, presets.RO_YELLOW, presets.RO_RED]
    layer = design.Layer("c", keys, "ceremony", flag, period_ms=10000, floor=0.6, background=0x50B0FF)
    r = design.Design("t", [layer]).renderer(ZONES)
    ins = layout.zone_of("Insert")
    sky_start = r.frame(0.0)[ins]
    assert sky_start & 0xFF > 0x80 and sky_start >> 16 > 0x40  # pale sky before the flag is raised
    up = r.frame(4.0)[ins]
    assert up & 0xFF > 0x80 and up >> 16 < 0x40  # flag blue while it flutters
    night = r.frame(9.2)[ins]
    assert (night & 0xFF) < (sky_start & 0xFF)  # the sky darkens for the fireworks
    assert r.frame(0.0) == r.frame(10.0)  # seamless loop


def test_best_fit_is_smooth_and_seamless():
    for make in (presets.romania_canada_design, presets.python_romania_design):
        d = make()
        frames, period, data, detail = design.fit_best(d, ZONES)
        assert period <= 40 and d.loop_ms() % period == 0 and len(data) <= rgb_store.LARGE_FILE_SIZE
