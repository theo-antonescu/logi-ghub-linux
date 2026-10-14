import json
import os
import time
from types import SimpleNamespace

import pytest

from logi_ghub import chatter, keyremap, profiles
from logi_ghub.actions import parse_action
from logi_ghub.daemon import Daemon, MacroRunner, Session

A, E, L, CAPS, LEFTCTRL, LEFTSHIFT = 30, 18, 38, 58, 29, 42
KB = "046d:40b6"


@pytest.fixture(autouse=True)
def clean_files():
    for path in (chatter.settings_path(), chatter.stats_path()):
        path.unlink(missing_ok=True)
    yield


def make_filter(ms=40, per_key=None, stats=None):
    return chatter.ChatterFilter(chatter.Settings(True, ms, per_key or {}), stats)


def feed(f, events):
    """events: (code, value, time in ms) -> the ones let through, without times."""
    return [(c, v) for c, v, t in events if f.allow(c, v, t / 1000)]


# --- the filter ---------------------------------------------------------------------------
def test_bounce_and_its_release_are_dropped():
    f = make_filter(40)
    out = feed(f, [(E, 1, 0), (E, 0, 60), (E, 1, 72), (E, 2, 80), (E, 0, 85), (A, 1, 90), (A, 0, 150)])
    assert out == [(E, 1), (E, 0), (A, 1), (A, 0)]


def test_real_double_letters_get_through():
    f = make_filter(40)
    out = feed(f, [(L, 1, 0), (L, 0, 70), (L, 1, 130), (L, 0, 200)])
    assert out == [(L, 1), (L, 0), (L, 1), (L, 0)]


def test_other_keys_in_between_do_not_reset_the_gap():
    f = make_filter(40)
    out = feed(f, [(E, 1, 0), (E, 0, 50), (A, 1, 55), (E, 1, 60), (E, 0, 70), (A, 0, 100)])
    assert out == [(E, 1), (E, 0), (A, 1), (A, 0)]


def test_per_key_limits_and_zero_means_never():
    f = make_filter(40, {"E": 80, "LeftShift": 0})
    out = feed(f, [(E, 1, 0), (E, 0, 50), (E, 1, 110), (E, 0, 150),  # 60 ms gap: blocked for E
                   (LEFTSHIFT, 1, 200), (LEFTSHIFT, 0, 250), (LEFTSHIFT, 1, 255), (LEFTSHIFT, 0, 300)])
    assert out == [(E, 1), (E, 0), (LEFTSHIFT, 1), (LEFTSHIFT, 0), (LEFTSHIFT, 1), (LEFTSHIFT, 0)]


def test_clock_going_backwards_never_blocks():
    f = make_filter(40)
    assert feed(f, [(E, 1, 1000), (E, 0, 1050), (E, 1, 10)]) == [(E, 1), (E, 0), (E, 1)]


# --- settings and counts ----------------------------------------------------------------------
def test_settings_round_trip_and_limits():
    chatter.save(KB, chatter.Settings(True, 55, {"E": 90}))
    chatter.save("046d:409f", chatter.Settings())
    assert chatter.load(KB) == chatter.Settings(True, 55, {"E": 90})
    assert chatter.load("046d:0000") == chatter.Settings()
    with pytest.raises(ValueError):
        chatter.Settings.from_dict({"threshold_ms": 5000})
    chatter.settings_path().write_text(json.dumps({KB: {"threshold_ms": -1}, "x": {"enabled": True}}))
    assert list(chatter.load_all()) == ["x"]  # the bad entry is skipped, not fatal


def test_stats_count_blocks_and_close_calls_and_survive_restarts():
    stats = chatter.Stats(KB)
    f = make_filter(40, stats=stats)
    feed(f, [(E, 1, 0), (E, 0, 50), (E, 1, 62), (E, 0, 70), (E, 1, 100), (E, 0, 140), (L, 1, 200)])
    stats.save()
    saved = chatter.load_stats()[KB]
    assert saved == {"E": {"blocked": 1, "blocked_min_ms": 12.0, "blocked_max_ms": 12.0,
                           "close": 1, "close_min_ms": 50.0}}
    again = chatter.Stats(KB)
    again.blocked(E, 3.0)
    again.save()
    assert chatter.load_stats()[KB]["E"]["blocked"] == 2
    assert chatter.load_stats()[KB]["E"]["blocked_min_ms"] == 3.0
    chatter.reset_stats(KB)
    assert chatter.load_stats() == {}


# --- in the keyboard grab -------------------------------------------------------------------
class FakeVirtual:
    def __init__(self):
        self.events, self.fd, self.closed = [], -1, False

    def key(self, code, down):
        self.events.append((code, down))

    def close(self):
        self.closed = True


class GrablessRemapper(keyremap.KeyRemapper):
    @staticmethod
    def _any_key_down(fd):
        return False

    def _grab(self, fd, on):
        pass


def run_remapper(tmp_path, mapping, events, want):
    fifo = tmp_path / "event3"
    os.mkfifo(fifo)
    v = FakeVirtual()
    r = GrablessRemapper("kb", [keyremap.EventNode(str(fifo), "kb", keys={A, E, CAPS})], mapping,
                         virtual_factory=lambda k, l: v, chatter=make_filter(40))
    r.start()
    w = os.open(fifo, os.O_WRONLY)
    ev = keyremap.EVENT
    os.write(w, b"".join(ev.pack(ms // 1000, (ms % 1000) * 1000, 1, code, value) for code, value, ms in events))
    deadline = time.time() + 2
    while len(v.events) < want and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    r.stop()
    os.close(w)
    return v.events


def test_filter_only_grab_passes_keys_through_without_the_bounce(tmp_path):
    events = [(E, 1, 5990), (E, 0, 6050), (E, 1, 6061), (E, 0, 6070), (A, 1, 6100), (A, 0, 6150)]
    assert run_remapper(tmp_path, {}, events, 4) == [(E, True), (E, False), (A, True), (A, False)]


def test_filter_runs_before_key_assignments(tmp_path):
    mapping = keyremap.resolve({"CapsLock": parse_action("key:LeftCtrl")})
    events = [(CAPS, 1, 0), (CAPS, 0, 80), (CAPS, 1, 95), (CAPS, 0, 120)]
    assert run_remapper(tmp_path, mapping, events, 2) == [(LEFTCTRL, True), (LEFTCTRL, False)]


# --- the daemon ------------------------------------------------------------------------------
class Recorder:
    """Stands in for keyremap.KeyRemapper in the daemon."""
    made: list = []

    def __init__(self, name, nodes, mapping, chatter=None, **kw):
        self.name, self.nodes, self.mapping, self.chatter = name, nodes, mapping, chatter
        self.running = self.panicked = False
        Recorder.made.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class Keyboard:
    """Just enough of a LogiDevice for Daemon.scan and Session."""

    def __init__(self, online=True):
        self.key = KB
        self.name = "G915 X LIGHTSPEED TKL"
        self.kind = "keyboard"
        self.online = online
        self.info = SimpleNamespace(path="/dev/hidraw4", behind_receiver=True)
        self.hidpp = SimpleNamespace(swid=0, add_handler=lambda h: None, start_listener=lambda: None)
        self.button_spy = self.onboard = self.dpi = self.report_rate = self.rgb = self.per_key = self.brightness = None
        self.closed = 0

    def binding_count(self):
        return 0

    def close(self):
        self.closed += 1


@pytest.fixture
def daemon(monkeypatch):
    Recorder.made = []
    monkeypatch.setattr(keyremap, "KeyRemapper", Recorder)
    monkeypatch.setattr(keyremap, "find_event_nodes", lambda path, rx: [keyremap.EventNode("/dev/input/event9", "kb")])
    monkeypatch.setattr(profiles, "get_active", lambda: {})
    d = Daemon()
    kb = Keyboard()
    d._devices = lambda: [kb]
    return d, kb


def test_daemon_filters_a_keyboard_without_a_host_profile(daemon):
    d, kb = daemon
    d.scan()
    assert Recorder.made == []  # off by default: the keyboard is left alone
    chatter.save(KB, chatter.Settings(True, 30))
    d.scan()
    (guard,) = Recorder.made
    assert guard.running and guard.mapping == {} and guard.chatter.default == 0.030
    assert kb.closed  # only the input device is kept, not HID++
    d.scan()
    assert len(Recorder.made) == 1  # already running

    guard.running = False  # unplugged
    d.scan()
    assert len(Recorder.made) == 2 and Recorder.made[1].running

    chatter.save(KB, chatter.Settings(False, 30))
    os.utime(chatter.settings_path(), (time.time() + 5, time.time() + 5))
    d.scan()
    assert not Recorder.made[1].running and d.guards == {} and len(Recorder.made) == 2


def test_daemon_stays_off_after_the_escape_keys_until_settings_change(daemon):
    d, kb = daemon
    chatter.save(KB, chatter.Settings(True))
    d.scan()
    guard = Recorder.made[0]
    guard.running, guard.panicked = False, True
    d.scan()
    assert len(Recorder.made) == 1
    chatter.save(KB, chatter.Settings(True, 50))
    os.utime(chatter.settings_path(), (time.time() + 5, time.time() + 5))
    d.scan()
    assert len(Recorder.made) == 2 and Recorder.made[1].chatter.default == 0.050


def test_host_session_runs_the_filter_in_its_own_grab(daemon, monkeypatch):
    d, kb = daemon
    chatter.save(KB, chatter.Settings(True))
    d.scan()
    guard = Recorder.made[0]
    monkeypatch.setattr(profiles, "get_active", lambda: {KB: "work"})
    monkeypatch.setattr(profiles, "load_profile", lambda key, name: profiles.Profile(name=name, device=key))
    d.scan()
    assert not guard.running and d.guards == {}
    session_grab = Recorder.made[1]
    assert session_grab.running and session_grab.chatter is not None and session_grab.mapping == {}
    assert d.sessions[KB].remapper is session_grab


def test_session_without_filter_or_assignments_leaves_keys_alone():
    kb = Keyboard()
    s = Session(kb, profiles.Profile(name="t", device=KB), None, MacroRunner(None), None, chatter_filter=lambda: None)
    s.ensure_keys()
    assert s.remapper is None
