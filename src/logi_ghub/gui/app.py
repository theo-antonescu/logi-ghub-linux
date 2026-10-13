"""GTK 4 / libadwaita front end."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import __version__, keyremap, keys, lightfx, profiles  # noqa: E402
from ..actions import Action  # noqa: E402
from ..device import LogiDevice, find_devices  # noqa: E402
from ..features import lighting  # noqa: E402
from ..features import onboard as ob  # noqa: E402
from ..macros import Macro  # noqa: E402
from .editors import ActionDialog, MacroDialog, confirm  # noqa: E402
from .keyboard import KeyboardView, int_to_rgba, rgba_to_int  # noqa: E402

log = logging.getLogger(__name__)
APP_ID = "io.github.theo_antonescu.LogiGhubLinux"

CSS = """
.lgl-key { min-height: 26px; min-width: 0; padding: 0 2px; font-size: 0.8em; }
.lgl-keyboard { padding: 6px; }
.lgl-dim { opacity: 0.6; }
"""


def layout_label(name: str) -> str:
    """Key name as printed on the keycap where that is clearer (Grave -> `)."""
    from .. import layout

    label = layout.LABELS.get(name)
    return f"{name} ({label})" if label and label != name and len(label) == 1 else name


def run_async(fn, done=None, error=None):
    """Run blocking device I/O off the UI thread and come back with the result."""

    def work():
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 - every failure is shown to the user
            log.debug("background task failed:\n%s", traceback.format_exc())
            if error:
                GLib.idle_add(error, e)
            return
        if done:
            GLib.idle_add(done, result)

    threading.Thread(target=work, daemon=True).start()


def daemon_running() -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ")
        except OSError:
            continue
        if b"logi-ghubd" in cmd or b"logi_ghub.daemon" in cmd or b"logi-ghub-cli daemon" in cmd:
            return True
    return False


def _clear(group_box: Gtk.Box):
    child = group_box.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        group_box.remove(child)
        child = nxt


class DevicePane(Gtk.Box):
    """Everything about one device: profiles, DPI, buttons, macros, lighting."""

    def __init__(self, win: "MainWindow", dev: LogiDevice):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.dev = dev
        self.slots: list[ob.OnboardProfile] = []
        self.active_slot = 0
        self.profile: profiles.Profile | None = None
        self.target: tuple[str, object] | None = None
        self.dirty = False
        self.dpi_values: list[int] = []
        self.device_brightness: int | None = None
        self.light_state = "onboard"
        self.onboard_effects: list[str] = []
        self.animator = None
        self.hz_values: list[int] = []
        self.paint_color = 0xFF0000

        self.banner = Adw.Banner(button_label="Save")
        self.banner.connect("button-clicked", lambda *_: self.save())
        self.append(self.banner)

        self.stack = Adw.ViewStack(vexpand=True)
        self.append(self.stack)
        self.pages: dict[str, Gtk.Box] = {}
        for name, title, icon in [
            ("profiles", "Profiles", "view-list-symbolic"),
            ("dpi", "Sensitivity", "input-mouse-symbolic"),
            ("buttons", "Assignments", "input-keyboard-symbolic"),
            ("macros", "Macros", "media-record-symbolic"),
            ("lighting", "Lighting", "weather-clear-symbolic"),
            ("info", "Device", "dialog-information-symbolic"),
        ]:
            page = Adw.PreferencesPage()
            self.stack.add_titled_with_icon(page, name, title, icon)
            self.pages[name] = page
        self.switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        if os.environ.get("LOGI_GHUB_PAGE"):
            GLib.idle_add(lambda: self.stack.set_visible_child_name(os.environ["LOGI_GHUB_PAGE"]) and False)
        self.groups: dict[str, list] = {k: [] for k in self.pages}
        self.load()

    # -- helpers ---------------------------------------------------------------------
    def toast(self, text: str):
        self.win.toast(text)

    def fail(self, e: Exception):
        self.win.toast(f"Error: {e}")

    def page_reset(self, name: str):
        page = self.pages[name]
        for g in self.groups[name]:
            page.remove(g)
        self.groups[name] = []

    def add_group(self, name: str, group: Adw.PreferencesGroup):
        self.pages[name].add(group)
        self.groups[name].append(group)
        return group

    def mark_dirty(self):
        self.dirty = True
        self.update_banner()

    def update_banner(self):
        if self.target is None:
            self.banner.set_revealed(False)
            return
        kind, ref = self.target
        where = f"on-board slot {ref}" if kind == "slot" else f"computer profile “{ref}”"
        state = "unsaved changes" if self.dirty else "saved"
        self.banner.set_title(f"Editing {where}: {self.profile.name} ({state})")
        self.banner.set_button_label("Save" if self.dirty else "")
        self.banner.set_revealed(True)

    # -- loading -----------------------------------------------------------------------
    def load(self):
        dev = self.dev

        def read():
            data = {}
            if dev.onboard:
                data["slots"] = dev.onboard.read_profiles()
                data["active"] = dev.onboard.get_current_profile()
                data["mode"] = dev.onboard.get_mode()
            if dev.dpi:
                data["dpi_values"] = dev.dpi.dpi_list()
            if dev.report_rate:
                data["hz"] = dev.report_rate.supported_hz()
            if dev.rgb:
                data["rgb"] = dev.rgb.zones
            if dev.per_key:
                data["keys"] = dev.per_key.zones
            if dev.brightness:
                data["brightness"] = dev.brightness.get()
            if isinstance(dev.rgb, lighting.RGBEffects):
                data["sw_control"] = dev.rgb.get_sw_control()[0]
                data["onboard_effects"] = dev.rgb.onboard_effect_names()
            data["info"] = dev.describe()
            return data

        run_async(read, self.loaded, self.fail)

    def loaded(self, data: dict):
        self.slots = data.get("slots", [])
        self.active_slot = data.get("active", 0)
        self.mode = data.get("mode", 0)
        self.dpi_values = data.get("dpi_values", [])
        self.hz_values = data.get("hz", [])
        self.info = data["info"]
        self.device_brightness = data.get("brightness")
        self.onboard_effects = data.get("onboard_effects", [])
        active_name = profiles.get_active().get(self.dev.key)
        applied = lightfx.last_applied(self.dev.key)
        if active_name and daemon_running():
            self.light_state = "service"
        elif data.get("sw_control") and applied is not None:
            self.light_state = "app"
        else:
            self.light_state = "onboard"
        if self.target is None:
            if self.light_state == "service" and not self.slots:
                try:
                    self.edit_computer(profiles.load_profile(self.dev.key, active_name))
                    return
                except (OSError, ValueError, KeyError):
                    pass
            if self.slots:
                active = next((p for p in self.slots if p.sector == self.active_slot), self.slots[0])
                self.edit_slot(active.sector)
            else:
                existing = profiles.list_profiles(self.dev.key)
                if existing:
                    self.edit_computer(existing[0])
                else:
                    self.profile = profiles.Profile(name="Default", device=self.dev.key)
                    self.target = ("computer", "Default")
                    self.dirty = True  # exists only in memory until saved
                if self.light_state == "app" and applied is not None and self.profile.lighting != applied:
                    # show what this app last put on the keyboard, which is what it is showing now
                    self.profile.lighting = applied
                    self.dirty = True
                self.rebuild()
        else:
            self.rebuild()

    def edit_slot(self, slot: int):
        p = self.slots[slot - 1]
        self.profile = profiles.Profile.from_onboard(self.dev.key, p, self.dev.binding_count(), None,
                                                     name=p.name or f"Slot {slot}")
        self.base_slot_macros(p)
        self.target = ("slot", slot)
        self.dirty = False
        self.rebuild()

    def base_slot_macros(self, p: ob.OnboardProfile):
        """Read macros a slot points to, in the background, and attach them to the working profile."""
        refs = {(a.sector, a.address) for g in (False, True) for a in p.buttons(16, g) if a.kind == "macro"}
        if not refs or not self.dev.onboard:
            return
        prof = self.profile

        def read():
            return {ref: self.dev.onboard.read_macro(*ref) for ref in refs}

        def done(found):
            if self.profile is not prof:
                return
            for (sector, address), ops in found.items():
                name = f"On-board macro {sector}"
                prof.macros.append(Macro.from_onboard_ops(name, ops))
                for layer in (prof.buttons, prof.gshift_buttons):
                    for i, a in layer.items():
                        if a.kind == "macro" and (a.sector, a.address) == (sector, address):
                            layer[i] = Action("macro", macro=name, sector=sector, address=address)
            self.rebuild()

        run_async(read, done, self.fail)

    def edit_computer(self, prof: profiles.Profile):
        self.profile = profiles.Profile.from_dict(prof.to_dict())
        self.target = ("computer", prof.name)
        self.dirty = False
        self.rebuild()

    def rebuild(self):
        self.build_profiles()
        self.build_dpi()
        self.build_buttons()
        self.build_macros()
        self.build_lighting()
        self.build_info()
        self.update_banner()

    # -- profiles page ------------------------------------------------------------------
    def build_profiles(self):
        self.page_reset("profiles")
        dev = self.dev
        if dev.onboard and self.slots:
            mode = {1: "on-board", 2: "host (software)"}.get(self.mode, "?")
            g = self.add_group("profiles", Adw.PreferencesGroup(
                title="On-board memory",
                description=f"Profiles stored in the {dev.kind}; they work on any computer without this app. Current mode: {mode}."))
            for p in self.slots:
                editing = self.target == ("slot", p.sector)
                hz = 1000 // max(1, p.report_rate_ms)
                row = Adw.ActionRow(title=GLib.markup_escape_text(p.name or f"Slot {p.sector}"),
                                    subtitle=f"Slot {p.sector} · {'/'.join(map(str, p.active_dpis))} DPI · {hz} Hz"
                                    + (" · active" if p.sector == self.active_slot else "")
                                    + ("" if p.enabled else " · disabled"))
                if p.sector == self.active_slot:
                    row.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))
                if editing:
                    row.add_css_class("accent")
                if p.sector != self.active_slot and p.enabled:
                    b = Gtk.Button(label="Activate", valign=Gtk.Align.CENTER)
                    b.connect("clicked", lambda _b, s=p.sector: self.activate_slot(s))
                    row.add_suffix(b)
                e = Gtk.Button(label="Editing" if editing else "Edit", valign=Gtk.Align.CENTER, sensitive=not editing)
                e.connect("clicked", lambda _b, s=p.sector: self.switch_target(lambda: self.edit_slot(s)))
                row.add_suffix(e)
                g.add(row)

        g = self.add_group("profiles", Adw.PreferencesGroup(
            title="On this computer",
            description="Profiles saved in ~/.config/logi-ghub-linux. Run one in host mode (the logi-ghubd service applies it), "
                        "or write it into an on-board slot."))
        new = Gtk.Button(label="Save current as…", valign=Gtk.Align.CENTER)
        new.connect("clicked", lambda *_: self.save_as_dialog())
        g.set_header_suffix(new)
        active = profiles.get_active().get(dev.key)
        stored = profiles.list_profiles(dev.key)
        if not stored:
            g.add(Adw.ActionRow(title="No computer profiles yet", subtitle="Use “Save current as…” to create one."))
        for prof in stored:
            if dev.kind == "keyboard":
                summary = f"{len(prof.key_actions)} key assignments · {len(prof.macros)} macros"
            else:
                summary = f"{'/'.join(map(str, prof.dpi_stages))} DPI · {len(prof.macros)} macros"
            row = Adw.ActionRow(title=GLib.markup_escape_text(prof.name), subtitle=summary)
            if dev.hidpp is not None:  # host mode needs HID++ (not available on the G600)
                sw = Gtk.Switch(active=prof.name == active, valign=Gtk.Align.CENTER, tooltip_text="Run in host mode")
                sw.connect("notify::active", lambda s, _p, n=prof.name: self.set_host(n if s.get_active() else None))
                row.add_suffix(Gtk.Label(label="Host mode", css_classes=["dim-label"]))
                row.add_suffix(sw)
            menu = Gio.Menu()
            for p in self.slots:
                menu.append(f"Write to on-board slot {p.sector}", f"pane.write-slot::{prof.name}\x1f{p.sector}")
            menu.append("Delete", f"pane.delete-profile::{prof.name}")
            mb = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER)
            row.add_suffix(mb)
            e = Gtk.Button(label="Edit", valign=Gtk.Align.CENTER, sensitive=self.target != ("computer", prof.name))
            e.connect("clicked", lambda _b, pr=prof: self.switch_target(lambda: self.edit_computer(pr)))
            row.add_suffix(e)
            g.add(row)
        if active and not daemon_running():
            warn = Adw.ActionRow(title="The logi-ghubd service is not running",
                                 subtitle="Host-mode profiles only work while it runs. Start it with: systemctl --user enable --now logi-ghubd")
            warn.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
            g.add(warn)

        if dev.onboard:
            g = self.add_group("profiles", Adw.PreferencesGroup(
                title="Backups", description="A full copy of the on-board memory is saved automatically before every write."))
            b = Adw.ButtonRow(title="Back up on-board memory now")
            b.connect("activated", lambda *_: self.backup_now())
            g.add(b)
            r = Adw.ButtonRow(title="Restore a backup…")
            r.connect("activated", lambda *_: self.restore_dialog())
            g.add(r)

        self.install_actions()

    def install_actions(self):
        group = Gio.SimpleActionGroup()
        a = Gio.SimpleAction.new("write-slot", GLib.VariantType.new("s"))
        a.connect("activate", lambda _a, v: self.write_profile_to_slot(*v.get_string().split("\x1f")))
        group.add_action(a)
        a = Gio.SimpleAction.new("delete-profile", GLib.VariantType.new("s"))
        a.connect("activate", lambda _a, v: self.delete_profile(v.get_string()))
        group.add_action(a)
        self.insert_action_group("pane", group)

    def switch_target(self, fn):
        if self.dirty:
            confirm(self.win, "Discard unsaved changes?", "The profile you are editing has changes that are not saved.",
                    "Discard", fn, destructive=True)
        else:
            fn()

    def activate_slot(self, slot: int):
        def work():
            if self.dev.onboard.get_mode() != ob.MODE_ONBOARD:
                self.dev.onboard.set_mode(ob.MODE_ONBOARD)
            self.dev.onboard.set_current_profile(slot)

        run_async(work, lambda _r: (self.toast(f"Slot {slot} is now active"), self.load()), self.fail)

    def set_host(self, name: str | None):
        profiles.set_active(self.dev.key, name)
        self.toast(f"“{name}” will run in host mode" if name else "Back to on-board profiles")
        self.build_profiles()

    def backup_now(self):
        run_async(lambda: ob.save_backup(self.dev.onboard, self.dev.key), lambda p: self.toast(f"Backup saved: {p.name}"), self.fail)

    def restore_dialog(self):
        fd = Gtk.FileDialog(title="Restore on-board memory", initial_folder=Gio.File.new_for_path(str(ob.backup_dir())))

        def picked(dialog, result):
            try:
                f = dialog.open_finish(result)
            except GLib.Error:
                return
            data = json.loads(f.load_contents()[1].decode())

            def go():
                def work():
                    safety = ob.save_backup(self.dev.onboard, self.dev.key)
                    return safety, self.dev.onboard.restore(data)

                run_async(work, lambda r: (self.toast(f"Restored {len(r[1])} sectors (previous state saved as {r[0].name})"),
                                           self.reload()), self.fail)

            confirm(self.win, "Restore on-board memory?",
                    f"This rewrites the {self.dev.kind}'s profiles with {f.get_basename()}. The current contents are backed up first.",
                    "Restore", go, destructive=True)

        fd.open(self.win, None, picked)

    def reload(self):
        self.target = None
        self.dirty = False
        self.load()

    def save_as_dialog(self):
        d = Adw.AlertDialog(heading="Save as computer profile", body="Name for the new profile")
        entry = Gtk.Entry(text=self.profile.name if self.profile else "")
        d.set_extra_child(entry)
        d.add_response("cancel", "Cancel")
        d.add_response("save", "Save")
        d.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r != "save" or not entry.get_text().strip():
                return
            prof = profiles.Profile.from_dict(self.profile.to_dict())
            prof.name = entry.get_text().strip()
            for layer in (prof.buttons, prof.gshift_buttons):
                for i, a in layer.items():
                    if a.kind == "macro":
                        layer[i] = Action.run_macro(a.macro)
            profiles.save_profile(prof)
            self.edit_computer(prof)
            self.toast(f"Saved “{prof.name}”")

        d.connect("response", resp)
        d.present(self.win)

    def delete_profile(self, name: str):
        def go():
            profiles.delete_profile(self.dev.key, name)
            if profiles.get_active().get(self.dev.key) == name:
                profiles.set_active(self.dev.key, None)
            if self.target == ("computer", name):
                self.target = None
                self.loaded({"slots": self.slots, "active": self.active_slot, "mode": self.mode,
                             "dpi_values": self.dpi_values, "hz": self.hz_values, "info": self.info})
            else:
                self.build_profiles()

        confirm(self.win, f"Delete “{name}”?", "The profile file is removed from this computer.", "Delete", go, destructive=True)

    def write_profile_to_slot(self, name: str, slot: str):
        prof = profiles.load_profile(self.dev.key, name)
        self._write_slot(prof, int(slot))

    def _write_slot(self, prof: profiles.Profile, slot: int, after=None):
        dev = self.dev

        def go():
            def work():
                backup = ob.save_backup(dev.onboard, dev.key)
                changes = profiles.write_to_onboard(dev.onboard, prof, slot, dev.binding_count())
                return backup, changes

            def done(r):
                self.toast(f"Written to slot {slot} ({len(r[1])} changes). Backup: {r[0].name}")
                if after:
                    after()
                self.reload()

            run_async(work, done, self.fail)

        confirm(self.win, f"Write to on-board slot {slot}?",
                f"This replaces slot {slot} of the {dev.name} with “{prof.name}” and enables the slot "
                f"(so the profile button cycles through it). "
                f"A backup of the whole on-board memory is saved first in {ob.backup_dir()}.", "Write", go)

    def save(self):
        if not self.profile or not self.target:
            return
        kind, ref = self.target
        if kind == "slot":
            def after():
                self.dirty = False
            self._write_slot(self.profile, int(ref), after)
        else:
            if ref != self.profile.name:
                profiles.delete_profile(self.dev.key, str(ref))
                if profiles.get_active().get(self.dev.key) == ref:
                    profiles.set_active(self.dev.key, self.profile.name)
            profiles.save_profile(self.profile)
            self.target = ("computer", self.profile.name)
            self.dirty = False
            self.toast(f"Saved “{self.profile.name}”")
            self.rebuild()

    # -- sensitivity page --------------------------------------------------------------
    def build_dpi(self):
        self.page_reset("dpi")
        page_visible = bool(self.dev.dpi and self.profile)
        self.stack.get_page(self.pages["dpi"]).set_visible(page_visible)
        if not page_visible:
            return
        p = self.profile
        lo, hi = (self.dpi_values[0], self.dpi_values[-1]) if self.dpi_values else (100, 25600)
        step = (self.dpi_values[1] - self.dpi_values[0]) if len(self.dpi_values) > 1 else 50
        max_stages = 4 if self.slots and hasattr(self.slots[0], "max_dpi_stages") else 5
        g = self.add_group("dpi", Adw.PreferencesGroup(title="DPI stages", description=f"Up to {max_stages} stages, {lo}–{hi} DPI. "
                                                       "The DPI buttons step through these."))
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Add a stage", sensitive=len(p.dpi_stages) < max_stages)
        add.connect("clicked", lambda *_: self._dpi_add())
        g.set_header_suffix(add)
        first_check = None
        for i, value in enumerate(p.dpi_stages):
            row = Adw.SpinRow.new_with_range(lo, hi, step)
            row.set_title(f"Stage {i + 1}")
            row.set_value(value)
            row.connect("notify::value", lambda r, _p, idx=i: self._dpi_changed(idx, int(r.get_value())))
            check = Gtk.CheckButton(active=i == p.default_dpi, tooltip_text="Default stage", valign=Gtk.Align.CENTER)
            if first_check is None:
                first_check = check
            else:
                check.set_group(first_check)
            check.connect("toggled", lambda c, idx=i: c.get_active() and self._dpi_default(idx))
            row.add_prefix(check)
            try_btn = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Try this DPI now")
            try_btn.connect("clicked", lambda _b, r=row: self._dpi_try(int(r.get_value())))
            row.add_suffix(try_btn)
            if len(p.dpi_stages) > 1:
                rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Remove stage")
                rm.add_css_class("flat")
                rm.connect("clicked", lambda _b, idx=i: self._dpi_remove(idx))
                row.add_suffix(rm)
            g.add(row)
        g2 = self.add_group("dpi", Adw.PreferencesGroup(title="DPI shift", description="Sensitivity while the sniper (DPI shift) button is held. On-board profiles store it as one of the stages."))
        labels = [f"{v} DPI (stage {i + 1})" for i, v in enumerate(p.dpi_stages)]
        shift = Adw.ComboRow(title="Shift to", model=Gtk.StringList.new(labels))
        if p.shift_dpi in p.dpi_stages:
            shift.set_selected(p.dpi_stages.index(p.shift_dpi))
        else:
            p.shift_dpi = p.dpi_stages[0]
        shift.connect("notify::selected", lambda r, _p: self._set(lambda: setattr(p, "shift_dpi", p.dpi_stages[r.get_selected()])))
        g2.add(shift)
        if self.hz_values:
            g3 = self.add_group("dpi", Adw.PreferencesGroup(title="Report rate"))
            values = sorted(self.hz_values)
            rate = Adw.ComboRow(title="Polling rate", model=Gtk.StringList.new([f"{v} Hz" for v in values]))
            closest = min(range(len(values)), key=lambda i: abs(values[i] - p.report_rate))
            rate.set_selected(closest)
            rate.connect("notify::selected", lambda r, _p: self._set(lambda: setattr(p, "report_rate", values[r.get_selected()])))
            g3.add(rate)

    def _set(self, fn):
        fn()
        self.mark_dirty()

    def _dpi_changed(self, idx, value):
        p = self.profile
        was_shift = p.shift_dpi == p.dpi_stages[idx]
        p.dpi_stages[idx] = value
        if was_shift:
            p.shift_dpi = value
        self.mark_dirty()

    def _dpi_default(self, idx):
        self.profile.default_dpi = idx
        self.mark_dirty()

    def _dpi_add(self):
        p = self.profile
        p.dpi_stages.append(min((p.dpi_stages[-1] if p.dpi_stages else 800) * 2, self.dpi_values[-1] if self.dpi_values else 25600))
        self.mark_dirty()
        self.build_dpi()

    def _dpi_remove(self, idx):
        p = self.profile
        removed = p.dpi_stages.pop(idx)
        p.default_dpi = min(p.default_dpi, len(p.dpi_stages) - 1)
        if p.shift_dpi == removed:
            p.shift_dpi = p.dpi_stages[0]
        self.mark_dirty()
        self.build_dpi()

    def _dpi_try(self, value):
        run_async(lambda: self.dev.dpi.set(value), lambda _r: self.toast(f"DPI is {value} until the next DPI button or profile change"), self.fail)

    # -- assignments page ---------------------------------------------------------------
    def build_buttons(self):
        self.page_reset("buttons")
        dev, p = self.dev, self.profile
        if self.remaps_keys():
            self.build_keys()
            return
        count = dev.binding_count()
        visible = bool(p and count)
        self.stack.get_page(self.pages["buttons"]).set_visible(visible)
        if not visible:
            return
        layers = [(False, "Buttons", "Click a button to change what it does.")]
        if dev.onboard and dev.onboard.info.has_gshift:
            layers.append((True, "G-Shift layer", "Second function of each button while the button set to G-Shift is held."))
        rom = None
        for gshift, title, desc in layers:
            g = self.add_group("buttons", Adw.PreferencesGroup(title=title, description=desc))
            layer = p.gshift_buttons if gshift else p.buttons
            for i in range(count):
                a = layer.get(i, Action("default"))
                row = Adw.ActionRow(title=dev.button_name(i), subtitle=GLib.markup_escape_text(a.label()), activatable=True)
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.connect("activated", lambda _r, idx=i, gs=gshift: self.edit_button(idx, gs))
                g.add(row)
        del rom

    def edit_button(self, i: int, gshift: bool):
        p = self.profile
        layer = p.gshift_buttons if gshift else p.buttons
        current = layer.get(i, Action("default"))

        def done(a: Action):
            layer[i] = a
            self.mark_dirty()
            self.build_buttons()

        title = self.dev.button_name(i) + (" (G-Shift)" if gshift else "")
        ActionDialog(title, current, [m.name for m in p.macros], done).present(self.win)

    # -- keyboard key assignments (host mode) -----------------------------------------------
    def remaps_keys(self) -> bool:
        return bool(self.profile) and self.dev.kind == "keyboard" and self.dev.hidpp is not None and not self.dev.binding_count()

    def build_keys(self):
        p = self.profile
        self.stack.get_page(self.pages["buttons"]).set_visible(True)
        g = self.add_group("buttons", Adw.PreferencesGroup(
            title="Keys",
            description="Click a key to change what it does. Key assignments are run by the logi-ghubd service: turn on "
                        "Host mode for this profile on the Profiles page. Holding Backspace + Esc + Enter together always "
                        "gives you the normal keyboard back."))
        view = KeyboardView(self.dev.per_key.zones if self.dev.per_key else None, None)
        view.paint_on_drag = False
        names = {zone: cap.name for zone, cap in view.buttons.items()}
        view.set_colors({zone: 0x3584E4 for zone, name in names.items() if name in p.key_actions})
        view.on_paint = lambda zone: self.edit_key(names[zone])
        g.add(view)

        g = self.add_group("buttons", Adw.PreferencesGroup(title="Assigned keys"))
        if not p.key_actions:
            g.add(Adw.ActionRow(title="Every key types normally"))
        for name, a in sorted(p.key_actions.items()):
            row = Adw.ActionRow(title=GLib.markup_escape_text(layout_label(name)), subtitle=GLib.markup_escape_text(a.label()),
                                activatable=True)
            reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Type normally again")
            reset.add_css_class("flat")
            reset.connect("clicked", lambda _b, n=name: self.reset_key(n))
            row.add_suffix(reset)
            row.connect("activated", lambda _r, n=name: self.edit_key(n))
            g.add(row)
        if p.key_actions and profiles.get_active().get(self.dev.key) != p.name:
            note = Adw.ActionRow(title="Not running", subtitle="Turn on Host mode for this profile on the Profiles page to use these keys.")
            note.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
            g.add(note)

    def edit_key(self, name: str):
        try:
            keyremap.key_code(name)
        except ValueError:
            self.toast(f"The {layout_label(name)} key is handled inside the keyboard and can't be reassigned")
            return
        p = self.profile
        current = p.key_actions.get(name, Action("default"))

        def done(a: Action):
            if a.kind == "default":
                p.key_actions.pop(name, None)
            else:
                p.key_actions[name] = a
            self.mark_dirty()
            self.build_buttons()

        ActionDialog(layout_label(name), current, [m.name for m in p.macros], done).present(self.win)

    def reset_key(self, name: str):
        self.profile.key_actions.pop(name, None)
        self.mark_dirty()
        self.build_buttons()

    # -- macros page ----------------------------------------------------------------------
    def build_macros(self):
        self.page_reset("macros")
        p = self.profile
        keyboard = self.remaps_keys()
        visible = bool(p and (self.dev.binding_count() or keyboard) and self.dev.hidpp is not None)  # G600: no macro support
        self.stack.get_page(self.pages["macros"]).set_visible(visible)
        if not visible:
            return
        if keyboard:
            note = "Macros on keys are played by the logi-ghubd service while this profile runs in host mode."
        else:
            note = ("On-board slots store macros in the device's spare memory sectors (short sequences only; "
                    "repeat modes need host mode).")
        g = self.add_group("macros", Adw.PreferencesGroup(title="Macros", description=note))
        add = Gtk.Button(label="New macro", valign=Gtk.Align.CENTER)
        add.connect("clicked", lambda *_: self.edit_macro(None))
        g.set_header_suffix(add)
        if not p.macros:
            g.add(Adw.ActionRow(title="No macros", subtitle="Create one, then assign it to a button on the Assignments page."))
        for m in p.macros:
            used = [self.dev.button_name(i) for i, a in p.buttons.items() if a.kind == "macro" and a.macro == m.name]
            used += [layout_label(k) for k, a in sorted(p.key_actions.items()) if a.kind == "macro" and a.macro == m.name]
            row = Adw.ActionRow(title=GLib.markup_escape_text(m.name),
                                subtitle=GLib.markup_escape_text(m.to_text()[:80] + (f" · on {', '.join(used)}" if used else "")),
                                activatable=True)
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, name=m.name: self.delete_macro(name))
            row.add_suffix(rm)
            row.connect("activated", lambda _r, mm=m: self.edit_macro(mm))
            g.add(row)

    def edit_macro(self, macro: Macro | None):
        p = self.profile

        def done(old_name, new: Macro):
            if old_name:
                p.macros = [new if m.name == old_name else m for m in p.macros]
                for layer in (p.buttons, p.gshift_buttons, p.key_actions):
                    for i, a in layer.items():
                        if a.kind == "macro" and a.macro == old_name:
                            layer[i] = Action.run_macro(new.name)
            else:
                p.macros.append(new)
            self.mark_dirty()
            self.build_macros()
            self.build_buttons()

        MacroDialog(macro, [m.name for m in p.macros], done).present(self.win)

    def delete_macro(self, name: str):
        p = self.profile
        p.macros = [m for m in p.macros if m.name != name]
        for layer in (p.buttons, p.gshift_buttons):
            for i, a in list(layer.items()):
                if a.kind == "macro" and a.macro == name:
                    layer[i] = Action("default")
        for k, a in list(p.key_actions.items()):
            if a.kind == "macro" and a.macro == name:
                del p.key_actions[k]
        self.mark_dirty()
        self.build_macros()
        self.build_buttons()

    # -- lighting page --------------------------------------------------------------------
    def build_lighting(self):
        self.page_reset("lighting")
        self._stop_preview()
        dev, p = self.dev, self.profile
        visible = bool(p and (dev.rgb or dev.per_key or dev.brightness or "led" in p.lighting))
        self.stack.get_page(self.pages["lighting"]).set_visible(visible)
        if not visible:
            return
        light = p.lighting
        if "led" in light and not dev.rgb:
            self._profile_led_group(light["led"])
            return
        light.setdefault("zones", {})

        g = self.add_group("lighting", Adw.PreferencesGroup(title="Lighting", description=self._lighting_state_text()))
        give_back = Gtk.Button(label="Hand back to keyboard", valign=Gtk.Align.CENTER,
                               tooltip_text="Stop controlling the lighting; the keyboard shows its own on-board effect again")
        give_back.connect("clicked", lambda *_: self.release_lighting())
        show = Gtk.Button(label="Show on keyboard", valign=Gtk.Align.CENTER, tooltip_text="Put this profile's lighting on the keyboard now")
        show.add_css_class("suggested-action")
        show.connect("clicked", lambda *_: self.lighting_changed(dirty=False))
        header = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        if self.light_state != "service":
            header.append(show)
        header.append(give_back)
        g.set_header_suffix(header)
        if dev.brightness:
            row = Adw.ActionRow(title="Brightness")
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, dev.brightness.min, dev.brightness.max, 1)
            scale.set_hexpand(True)
            scale.set_size_request(240, -1)
            scale.set_value(light.get("brightness", self.device_brightness if self.device_brightness is not None else dev.brightness.max))
            scale.connect("value-changed", lambda s: self._brightness(int(s.get_value())))
            row.add_suffix(scale)
            g.add(row)

        self._keyboard_group()
        if dev.per_key:
            self._animation_group()
        for zone in (dev.rgb.zones if dev.rgb else []):
            self._zone_group(zone, light["zones"])
        self._start_preview()

    def _profile_led_group(self, led: dict):
        """Mice that keep their lighting inside each on-board profile (G600)."""
        from ..features.g600 import LED_EFFECTS

        g = self.add_group("lighting", Adw.PreferencesGroup(
            title="Mouse lighting", description="Saved with this profile; it shows on the mouse once the profile is saved."))
        effects = list(LED_EFFECTS)
        combo = Adw.ComboRow(title="Effect", model=Gtk.StringList.new([LED_EFFECTS[e] for e in effects] + ["Off"]))
        off = led.get("color", 0) == 0 and led.get("effect") == 0
        combo.set_selected(len(effects) if off else effects.index(led.get("effect", 0)) if led.get("effect", 0) in effects else 0)
        g.add(combo)
        color_row = Adw.ActionRow(title="Colour")
        color_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        color_btn.set_rgba(int_to_rgba(led.get("color", 0xFFFFFF) or 0xFFFFFF))
        color_row.add_suffix(color_btn)
        g.add(color_row)
        duration = Adw.SpinRow.new_with_range(1, 15, 1)
        duration.set_title("Effect speed (seconds)")
        duration.set_value(led.get("duration", 4) or 4)
        g.add(duration)
        gshift_row = Adw.ActionRow(title="Colour while G-Shift is held")
        gshift_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        gshift_btn.set_rgba(int_to_rgba(led.get("gshift_color", 0xFFFFFF)))
        gshift_row.add_suffix(gshift_btn)
        g.add(gshift_row)

        def changed(*_):
            i = combo.get_selected()
            is_off = i >= len(effects)
            led["effect"] = 0 if is_off else effects[i]
            led["color"] = 0 if is_off else rgba_to_int(color_btn.get_rgba())
            led["duration"] = int(duration.get_value())
            led["gshift_color"] = rgba_to_int(gshift_btn.get_rgba())
            color_row.set_sensitive(not is_off)
            duration.set_sensitive(not is_off and led["effect"] != 0)
            self.mark_dirty()

        changed_quiet = changed
        color_row.set_sensitive(not off)
        duration.set_sensitive(not off and led.get("effect", 0) != 0)
        for w, sig in ((combo, "notify::selected"), (color_btn, "notify::rgba"), (duration, "notify::value"), (gshift_btn, "notify::rgba")):
            w.connect(sig, changed_quiet)

    def _lighting_state_text(self) -> str:
        if self.light_state == "service":
            return "The logi-ghubd service is running this profile; saving applies your changes to the keyboard."
        if self.light_state == "onboard":
            names = ", ".join(f"“{n}”" for n in self.onboard_effects) or "its default effect"
            return (f"The keyboard is showing its own on-board effect ({names}, stored by G HUB). Keyboards can't report "
                    "those colours back, so the picture shows this profile. Any change here takes over the lighting.")
        return ("This is what the keyboard is showing. Changes appear right away and last until the keyboard sleeps, "
                "unless the profile runs in host mode (then the service keeps it, including animations).")

    def _keyboard_group(self):
        p = self.profile
        g = self.add_group("lighting", Adw.PreferencesGroup(
            title="Keys", description="Pick a colour, then click or drag over keys to paint them."))
        picker = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        picker.set_rgba(int_to_rgba(self.paint_color))
        picker.connect("notify::rgba", lambda b, _p: setattr(self, "paint_color", rgba_to_int(b.get_rgba())))
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.append(picker)
        fill = Gtk.Button(label="Fill all")
        clear = Gtk.Button(label="Clear")
        box.append(fill)
        box.append(clear)
        if self.dev.per_key:
            g.set_header_suffix(box)
        self.kb_view = KeyboardView(self.dev.per_key.zones if self.dev.per_key else None, None)
        painted = p.lighting.setdefault("keys", {})

        def ensure_static_base():
            zones = p.lighting["zones"]
            if self.dev.rgb and zones.get("0", {}).get("effect") != 0x01 and not (p.lighting.get("animation") or {}).get("enabled"):
                zones["0"] = lighting.Effect(0x01, color=0xFFFFFF).to_dict()
                self.build_lighting_later()

        def paint(zone):
            painted[str(zone)] = self.paint_color
            ensure_static_base()
            self.lighting_changed()

        def fill_all(*_):
            for z in self.kb_view.buttons:
                painted[str(z)] = self.paint_color
            ensure_static_base()
            self.lighting_changed()

        def clear_all(*_):
            painted.clear()
            self.lighting_changed()

        if self.dev.per_key:
            self.kb_view.on_paint = paint
        fill.connect("clicked", fill_all)
        clear.connect("clicked", clear_all)
        g.add(self.kb_view)

    def _animation_group(self):
        light = self.profile.lighting
        anim = light.get("animation") or lightfx.default_animation()
        g = self.add_group("lighting", Adw.PreferencesGroup(
            title="Colour sequence",
            description="Cycle the keys through your own colours, in your order. Runs from the computer: "
                        "it plays while this app is open, or always when the profile runs in host mode."))
        enabled = Adw.SwitchRow(title="Play a colour sequence", active=bool(anim.get("enabled")))
        g.add(enabled)

        colors_row = Adw.ActionRow(title="Colours, in order")
        colors_box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        colors_row.add_suffix(colors_box)
        g.add(colors_row)
        step = Adw.SpinRow.new_with_range(50, 30000, 50)
        step.set_title("Time per colour (ms)")
        step.set_value(anim.get("step_ms", 1000))
        g.add(step)
        fade = Adw.SwitchRow(title="Fade between colours", active=anim.get("fade", True))
        g.add(fade)
        pattern_keys = list(lightfx.PATTERNS)
        pattern = Adw.ComboRow(title="Pattern", model=Gtk.StringList.new(list(lightfx.PATTERNS.values())))
        pattern.set_selected(pattern_keys.index(anim.get("pattern", "all")) if anim.get("pattern", "all") in pattern_keys else 0)
        g.add(pattern)
        keep = Adw.SwitchRow(title="Keep painted keys", subtitle="Keys you painted stay their colour while the rest animate",
                             active=anim.get("keep_painted", True))
        g.add(keep)
        colors = [int(c) for c in anim.get("colors", [])] or [0xFF0000]

        def store(*_):
            light["animation"] = {
                "enabled": enabled.get_active(), "colors": list(colors), "step_ms": int(step.get_value()),
                "fade": fade.get_active(), "pattern": pattern_keys[pattern.get_selected()], "keep_painted": keep.get_active(),
            }
            for w in (colors_row, step, fade, pattern, keep):
                w.set_sensitive(enabled.get_active())
            self.lighting_changed()

        def rebuild_colors():
            child = colors_box.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                colors_box.remove(child)
                child = nxt
            for i, c in enumerate(colors):
                b = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), tooltip_text=f"Colour {i + 1}")
                b.set_rgba(int_to_rgba(c))
                b.connect("notify::rgba", lambda btn, _p, idx=i: (colors.__setitem__(idx, rgba_to_int(btn.get_rgba())), store()))
                colors_box.append(b)
            if len(colors) > 1:
                rm = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove the last colour")
                rm.add_css_class("flat")
                rm.connect("clicked", lambda *_: (colors.pop(), rebuild_colors(), store()))
                colors_box.append(rm)
            if len(colors) < 12:
                add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a colour")
                add.add_css_class("flat")
                add.connect("clicked", lambda *_: (colors.append(self.paint_color), rebuild_colors(), store()))
                colors_box.append(add)

        rebuild_colors()
        for w in (colors_row, step, fade, pattern, keep):
            w.set_sensitive(enabled.get_active())
        enabled.connect("notify::active", store)
        step.connect("notify::value", store)
        fade.connect("notify::active", store)
        pattern.connect("notify::selected", store)
        keep.connect("notify::active", store)

    def _zone_group(self, zone: lighting.Zone, zones: dict):
        stored = zones.get(str(zone.index))
        current = lighting.Effect.from_dict(stored) if stored else lighting.Effect(0x01, color=0xFFFFFF)
        title = "Whole keyboard effect" if zone.index == 0 else f"{zone.name} effect"
        desc = "Effects the keyboard runs by itself. Painted keys show over Static." if zone.index == 0 else None
        g = self.add_group("lighting", Adw.PreferencesGroup(title=title, description=desc))
        effects = [e for e in zone.effects if e.effect_id in lighting.EFFECTS]
        ids = [e.effect_id for e in effects]
        labels = [e.name for e in effects]
        combo = Adw.ComboRow(title="Effect", model=Gtk.StringList.new(labels + ([] if stored else ["(not set)"])))
        combo.set_selected(ids.index(current.effect_id) if stored and current.effect_id in ids else len(labels) if not stored else 0)
        g.add(combo)
        color_row = Adw.ActionRow(title="Colour")
        color_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        color_btn.set_rgba(int_to_rgba(current.color))
        color_row.add_suffix(color_btn)
        g.add(color_row)
        period = Adw.SpinRow.new_with_range(100, 20000, 100)
        period.set_title("Speed (period, ms)")
        period.set_value(current.period)
        g.add(period)
        intensity = Adw.SpinRow.new_with_range(1, 100, 1)
        intensity.set_title("Intensity %")
        intensity.set_value(current.intensity)
        g.add(intensity)
        direction = Adw.ComboRow(title="Direction", model=Gtk.StringList.new(list(lighting.DIRECTIONS.values())))
        direction.set_selected(current.direction if current.direction in lighting.DIRECTIONS else 1)
        g.add(direction)

        def selected_id():
            i = combo.get_selected()
            return ids[i] if i < len(ids) else None

        def sync_visibility():
            eid = selected_id()
            params = lighting.EFFECTS[eid][1] if eid is not None else {}
            color_row.set_visible("color" in params)
            period.set_visible("period" in params)
            intensity.set_visible("intensity" in params)
            direction.set_visible("direction" in params)

        def changed(*_):
            sync_visibility()
            eid = selected_id()
            if eid is None:
                zones.pop(str(zone.index), None)
            else:
                zones[str(zone.index)] = lighting.Effect(eid, color=rgba_to_int(color_btn.get_rgba()), period=int(period.get_value()),
                                                         intensity=int(intensity.get_value()), direction=direction.get_selected()).to_dict()
            self.lighting_changed()

        sync_visibility()
        combo.connect("notify::selected", changed)
        color_btn.connect("notify::rgba", changed)
        period.connect("notify::value", changed)
        intensity.connect("notify::value", changed)
        direction.connect("notify::selected", changed)

    # preview and live application
    def build_lighting_later(self):
        GLib.idle_add(lambda: self.build_lighting() and False)

    def _start_preview(self):
        self._stop_preview()
        if not getattr(self, "kb_view", None):
            return
        self.renderer = lightfx.Renderer(self.profile.lighting, self.dev.per_key.zones if self.dev.per_key else None,
                                         len(self.dev.rgb.zones) if self.dev.rgb else 1)
        self._t0 = GLib.get_monotonic_time() / 1e6
        if self.light_state == "onboard" and not self.dirty:
            self.kb_view.set_colors(self.renderer.frame(0.0))
            return

        def tick():
            if self.stack.get_visible_child_name() == "lighting":
                self.kb_view.set_colors(self.renderer.frame(GLib.get_monotonic_time() / 1e6 - self._t0))
            return True

        tick()
        if self.renderer.is_animated():
            self._preview_source = GLib.timeout_add(66, tick)

    def _stop_preview(self):
        src = getattr(self, "_preview_source", None)
        if src:
            GLib.source_remove(src)
            self._preview_source = None

    def lighting_changed(self, dirty: bool = True):
        if dirty:
            self.mark_dirty()
        self._start_preview()
        if self.light_state == "service":
            return  # the service owns the keyboard; it picks the change up when the profile is saved
        if self.light_state != "app":
            self.light_state = "app"
            self.build_lighting_later()
        if getattr(self, "_apply_pending", None):
            GLib.source_remove(self._apply_pending)
        self._apply_pending = GLib.timeout_add(200, self._apply_live)

    def _apply_live(self):
        self._apply_pending = None
        light = json.loads(json.dumps(self.profile.lighting))
        dev = self.dev

        def work():
            old = getattr(self, "animator", None)
            if old:
                old.stop()
                old.join(timeout=1)
            claimed = getattr(self, "_claimed", False)
            anim = lightfx.apply(dev, light, claim=not claimed)
            self._claimed = True
            return anim

        run_async(work, lambda anim: setattr(self, "animator", anim), self.fail)
        return False

    def _brightness(self, value: int):
        self.profile.lighting["brightness"] = value
        self.mark_dirty()
        run_async(lambda: self.dev.brightness.set(value), None, self.fail)

    def stop_lighting(self):
        anim = getattr(self, "animator", None)
        if anim:
            anim.stop()
            self.animator = None
        self._stop_preview()

    def release_lighting(self):
        self.stop_lighting()

        def work():
            if self.dev.rgb:
                self.dev.rgb.release()
            lightfx.clear_applied(self.dev.key)
            self._claimed = False

        def done(_r):
            self.light_state = "onboard"
            self.toast("Lighting handed back to the keyboard")
            self.build_lighting()

        run_async(work, done, self.fail)

    # -- info page --------------------------------------------------------------------------
    def build_info(self):
        self.page_reset("info")
        info = self.info
        g = self.add_group("info", Adw.PreferencesGroup(title=self.dev.name))
        rows = [("Model", info.get("model", "")), ("Connection", self.dev.info.path),
                ("Protocol", f"HID++ {info.get('protocol', '?')}")]
        rows += [("Firmware", f) for f in info.get("firmware", [])]
        if "battery" in info:
            b = info["battery"]
            rows.append(("Battery", f"{b['percent']}% ({b['charging']})" if b["percent"] else b["level"]))
        for title, value in rows:
            r = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(str(value)))
            r.add_css_class("property")
            g.add(r)
        g2 = self.add_group("info", Adw.PreferencesGroup(title="HID++ features"))
        expander = Adw.ExpanderRow(title=f"{len(info.get('features', {}))} features")
        for name, idx in info.get("features", {}).items():
            expander.add_row(Adw.ActionRow(title=name, subtitle=f"index {idx}"))
        g2.add(expander)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, fake: bool):
        super().__init__(application=app, title="Logitech G devices", default_width=1100, default_height=760)
        self.fake = fake
        self.devices: list[LogiDevice] = []
        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=240)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        side = Adw.ToolbarView()
        sh = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Look for devices again")
        refresh.connect("clicked", lambda *_: self.scan())
        sh.pack_start(refresh)
        menu = Gio.Menu()
        menu.append("About", "app.about")
        sh.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        side.add_top_bar(sh)
        self.list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.list.connect("row-selected", self.on_select)
        side.set_content(self.list)
        self.split.set_sidebar(Adw.NavigationPage(title="Devices", child=side))

        self.content_view = Adw.ToolbarView()
        self.content_header = Adw.HeaderBar()
        self.content_view.add_top_bar(self.content_header)
        self.status = Adw.StatusPage(icon_name="input-mouse-symbolic", title="Looking for devices…")
        self.content_view.set_content(self.status)
        self.content_page = Adw.NavigationPage(title="Device", child=self.content_view)
        self.split.set_content(self.content_page)
        self.panes: dict[str, DevicePane] = {}
        self.scan()

    def toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=5))

    def scan(self):
        for d in self.devices:
            d.close()
        self.panes.clear()

        def find():
            if self.fake:
                from ..hidpp.fake import fake_devices

                return fake_devices()
            return find_devices()

        run_async(find, self.found, lambda e: self.toast(f"Error: {e}"))

    def found(self, devices: list[LogiDevice]):
        self.devices = devices
        _clear(self.list)
        if not devices:
            self.status.set_title("No Logitech devices found")
            self.status.set_description("Check that the receiver is plugged in and the udev rule is installed (see README).")
            self.content_view.set_content(self.status)
            return
        for d in devices:
            icon = "input-keyboard-symbolic" if d.kind == "keyboard" else "input-mouse-symbolic"
            row = Adw.ActionRow(title=GLib.markup_escape_text(d.name))
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            if d.online:
                b = d.battery()
                row.set_subtitle(f"{b.percent}% battery" if b and b.percent else "connected")
            else:
                row.set_subtitle("asleep or off: press a key / move it, then refresh")
                row.add_css_class("lgl-dim")
            row.dev = d
            self.list.append(row)
        want = os.environ.get("LOGI_GHUB_DEVICE")
        first = next((i for i, d in enumerate(devices) if d.online and (not want or want in (d.key, d.kind))), None)
        if first is None:
            first = next((i for i, d in enumerate(devices) if d.online), 0)
        self.list.select_row(self.list.get_row_at_index(first))

    def on_select(self, _list, row):
        if row is None:
            return
        dev: LogiDevice = row.get_child().dev if not hasattr(row, "dev") else row.dev
        if not dev.online:
            self.status.set_title(f"{dev.name} is not reachable")
            self.status.set_description(dev.error or "")
            self.content_view.set_content(self.status)
            self.content_header.set_title_widget(None)
            return
        pane = self.panes.get(dev.info.path)
        if pane is None:
            pane = DevicePane(self, dev)
            self.panes[dev.info.path] = pane
        self.content_view.set_content(pane)
        self.content_header.set_title_widget(pane.switcher)
        self.content_page.set_title(dev.name)
        self.split.set_show_content(True)


class App(Adw.Application):
    def __init__(self, fake: bool):
        super().__init__(application_id=APP_ID + (".Fake" if fake else ""), flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.fake = fake
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self.about)
        self.add_action(about)

    def do_activate(self):
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = self.props.active_window or MainWindow(self, self.fake)
        win.present()

    def about(self, *_):
        d = Adw.AboutDialog(application_name="logi-ghub-linux", version=__version__, license_type=Gtk.License.GPL_2_0,
                            comments="DPI, buttons, macros, lighting and on-board profiles for Logitech G devices.",
                            website="https://github.com/theo-antonescu/logi-ghub-linux",
                            developers=["Theo Antonescu", "Claude"])
        d.present(self.props.active_window)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="logi-ghub")
    ap.add_argument("--fake", action="store_true", help="use simulated devices")
    ap.add_argument("-v", "--verbose", action="store_true")
    args, rest = ap.parse_known_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    if args.fake:
        from ..config import use_sandbox

        use_sandbox()
    Adw.init()
    return App(args.fake).run([sys.argv[0]] + rest)


if __name__ == "__main__":
    sys.exit(main())
