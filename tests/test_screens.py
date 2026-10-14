from logi_ghub import screens


def test_move_mouse_to_lands_on_the_primary_monitor(monkeypatch):
    # three 2560x1440 monitors side by side, the primary on the left
    layout = [((5120, 0, 2560, 1440), False), ((2560, 0, 2560, 1440), False), ((0, 0, 2560, 1440), True)]
    monkeypatch.setattr(screens, "_layout", lambda: (layout, 3))
    area = screens.primary_fraction()
    assert screens.to_desktop(32767, 32767, area) == (10922, 32767)
    assert screens.to_desktop(0, 0, area) == (0, 0)
    assert screens.to_desktop(65535, 65535, area) == (21845, 65535)


def test_one_monitor_or_no_layout_covers_the_whole_desktop(monkeypatch):
    monkeypatch.setattr(screens, "_layout", lambda: ([((0, 0, 1920, 1080), True)], 1))
    assert screens.primary_fraction() is None

    def fail():
        raise OSError("no session bus")

    monkeypatch.setattr(screens, "_layout", fail)
    assert screens.primary_fraction() is None
    assert screens.to_desktop(100, 200, None) == (100, 200)
