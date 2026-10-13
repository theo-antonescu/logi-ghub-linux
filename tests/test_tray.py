import pytest

gi = pytest.importorskip("gi")


def test_menu_lists_devices_slots_and_host_profiles(monkeypatch):
    from logi_ghub import profiles, tray

    st = tray.DeviceState("046d:409f", "G502 X", "mouse")
    st.online, st.battery, st.slots, st.active_slot = True, 40, [(1, "Slot 1"), (2, "Gaming")], 2
    profiles.save_profile(profiles.Profile(name="Desk", device="046d:409f"))
    t = tray.Tray.__new__(tray.Tray)
    t.states = {st.key: st}
    t.loop = None
    root = t.build_menu()
    dev = root.children[0]
    assert dev.label.startswith("G502 X") and "40%" in dev.label
    labels = [(c.label, c.radio) for c in dev.children]
    assert ("Gaming", True) in labels and ("Slot 1", False) in labels
    assert ("Desk", False) in labels and ("Off", True) in labels
    assert "40%" in t.tooltip_text()
