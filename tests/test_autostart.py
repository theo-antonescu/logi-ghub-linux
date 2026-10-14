from logi_ghub import autostart


def test_enable_and_disable_writes_autostart_entry(monkeypatch):
    calls = []
    monkeypatch.setattr(autostart, "_systemctl", lambda *a: calls.append(a) or True)
    monkeypatch.setattr(autostart, "service_installed", lambda: True)
    monkeypatch.setattr(autostart, "tray_running", lambda: True)
    assert not autostart.is_enabled()
    assert autostart.set_enabled(True) == []
    assert autostart.is_enabled()
    text = autostart._autostart_file().read_text()
    assert "Exec=" in text and "logi-ghub-tray" in text
    autostart.set_enabled(False)
    assert not autostart.is_enabled()
    assert calls == [("enable", "--now", "logi-ghubd"), ("disable", "--now", "logi-ghubd")]


def test_process_running_reads_command_lines():
    assert autostart.process_running(b"pytest")  # this test run
    assert not autostart.process_running(b"no-such-program-\x01")
