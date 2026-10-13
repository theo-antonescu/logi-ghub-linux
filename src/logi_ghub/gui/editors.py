"""Dialogs for choosing a button action and editing a macro."""

from __future__ import annotations

import time

from gi.repository import Adw, Gdk, Gtk

from .. import keys
from ..actions import SPECIALS, Action
from ..macros import REPEAT_MODES, Macro, Step

KIND_LABELS = [
    ("default", "Factory default"),
    ("none", "Disabled"),
    ("mouse", "Mouse button"),
    ("key", "Keystroke"),
    ("consumer", "Media key"),
    ("special", "Device function"),
    ("macro", "Macro"),
]
REPEAT_LABELS = {"once": "Play once", "while_held": "Repeat while held", "toggle": "Toggle repeat on press"}


def _string_list(items: list[str]) -> Gtk.StringList:
    return Gtk.StringList.new(items)


def keycode_to_name(keycode: int) -> str | None:
    """GDK hardware keycodes are evdev codes + 8 on Linux, so this follows the physical key."""
    return keys.EVDEV_TO_NAME.get(keycode - 8)


class ChordCapture(Gtk.Button):
    """Click, then press a key combination; reports (modifier mask, HID usage)."""

    def __init__(self, on_change):
        super().__init__(label="Click and press keys…")
        self.on_change = on_change
        self.capturing = False
        self.mods = 0
        ctl = Gtk.EventControllerKey()
        ctl.connect("key-pressed", self._pressed)
        self.add_controller(ctl)
        self.connect("clicked", self._start)

    def _start(self, *_):
        self.capturing = True
        self.mods = 0
        self.set_label("Press the key combination…")
        self.grab_focus()

    def _pressed(self, _ctl, _keyval, keycode, _state):
        if not self.capturing:
            return False
        name = keycode_to_name(keycode)
        if name is None:
            return True
        if name in keys.MODIFIERS:
            self.mods |= 1 << keys.MODIFIERS.index(name)
            self.set_label(keys.modifiers_label(self.mods) + "+…")
            return True
        self.capturing = False
        self.on_change(self.mods, keys.hid_for(name))
        return True

    def show(self, mods: int, hid: int) -> None:
        self.set_label(keys.key_label(mods, hid))


class ActionDialog(Adw.Dialog):
    def __init__(self, title: str, action: Action, macros: list[str], on_done, allow_default: bool = True):
        super().__init__(title=title, content_width=460)
        self.on_done = on_done
        self.action = Action.from_dict(action.to_dict())
        self.kinds = [k for k in KIND_LABELS if allow_default or k[0] != "default"]
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        ok = Gtk.Button(label="Set")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", self._ok)
        header.pack_start(cancel)
        header.pack_end(ok)
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        page.add(group)
        toolbar.set_content(page)
        self.set_child(toolbar)

        self.kind_row = Adw.ComboRow(title="Action", model=_string_list([l for _, l in self.kinds]))
        group.add(self.kind_row)

        self.mouse_row = Adw.ComboRow(title="Button", model=_string_list([v[0] for v in keys.MOUSE_BUTTONS.values()]))
        group.add(self.mouse_row)

        self.key_row = Adw.ActionRow(title="Keys")
        self.capture = ChordCapture(self._chord)
        self.capture.set_valign(Gtk.Align.CENTER)
        self.key_row.add_suffix(self.capture)
        group.add(self.key_row)
        self.key_list_row = Adw.ComboRow(title="…or pick a key", model=_string_list(keys.KEY_NAMES), enable_search=True)
        self.key_list_row.connect("notify::selected", self._picked_key)
        group.add(self.key_list_row)
        self.mod_rows = []
        mods_group = Adw.PreferencesGroup(title="Modifiers")
        for i, label in enumerate(keys.MODIFIER_LABELS[:4]):
            r = Adw.SwitchRow(title=label)
            r.connect("notify::active", self._mods_changed)
            mods_group.add(r)
            self.mod_rows.append(r)
        self.mods_group = mods_group
        page.add(mods_group)

        self.consumer_row = Adw.ComboRow(title="Media key", model=_string_list(keys.CONSUMER_NAMES))
        group.add(self.consumer_row)
        self.special_codes = list(SPECIALS)
        self.special_row = Adw.ComboRow(title="Function", model=_string_list([SPECIALS[c] for c in self.special_codes]))
        group.add(self.special_row)
        self.profile_row = Adw.SpinRow.new_with_range(1, 5, 1)
        self.profile_row.set_title("Profile number")
        group.add(self.profile_row)
        self.macros = macros
        self.macro_row = Adw.ComboRow(title="Macro", model=_string_list(macros or ["(no macros yet: add one on the Macros page)"]))
        group.add(self.macro_row)

        self._load()
        self.kind_row.connect("notify::selected", lambda *_: self._sync())
        self.special_row.connect("notify::selected", lambda *_: self._sync())
        self._sync()

    def _load(self):
        a = self.action
        kinds = [k for k, _ in self.kinds]
        self.kind_row.set_selected(kinds.index(a.kind) if a.kind in kinds else 0)
        if a.kind == "mouse":
            self.mouse_row.set_selected(max(0, a.button - 1))
        if a.kind == "key":
            self.capture.show(a.modifiers, a.key)
            if a.key in keys.HID_TO_NAME:
                self.key_list_row.set_selected(keys.KEY_NAMES.index(keys.HID_TO_NAME[a.key]))
            for i, r in enumerate(self.mod_rows):
                r.set_active(bool(a.modifiers & (1 << i)))
        if a.kind == "consumer" and a.usage in keys.CONSUMER_TO_NAME:
            self.consumer_row.set_selected(keys.CONSUMER_NAMES.index(keys.CONSUMER_TO_NAME[a.usage]))
        if a.kind == "special" and a.function in self.special_codes:
            self.special_row.set_selected(self.special_codes.index(a.function))
            self.profile_row.set_value(max(1, a.data))
        if a.kind == "macro" and a.macro in self.macros:
            self.macro_row.set_selected(self.macros.index(a.macro))

    def _chord(self, mods: int, hid: int):
        self.action.modifiers, self.action.key = mods, hid
        self.capture.show(mods, hid)
        for i, r in enumerate(self.mod_rows):
            r.set_active(bool(mods & (1 << i)))
        if hid in keys.HID_TO_NAME:
            self.key_list_row.set_selected(keys.KEY_NAMES.index(keys.HID_TO_NAME[hid]))

    def _picked_key(self, *_):
        self.action.key = keys.hid_for(keys.KEY_NAMES[self.key_list_row.get_selected()])
        self.capture.show(self.action.modifiers, self.action.key)

    def _mods_changed(self, *_):
        mask = self.action.modifiers & 0xF0
        for i, r in enumerate(self.mod_rows):
            if r.get_active():
                mask |= 1 << i
        self.action.modifiers = mask
        self.capture.show(self.action.modifiers, self.action.key)

    def _sync(self):
        kind = self.kinds[self.kind_row.get_selected()][0]
        self.mouse_row.set_visible(kind == "mouse")
        for w in (self.key_row, self.key_list_row, self.mods_group):
            w.set_visible(kind == "key")
        self.consumer_row.set_visible(kind == "consumer")
        self.special_row.set_visible(kind == "special")
        code = self.special_codes[self.special_row.get_selected()]
        self.profile_row.set_visible(kind == "special" and code == 0x0D)
        self.macro_row.set_visible(kind == "macro")

    def _ok(self, *_):
        kind = self.kinds[self.kind_row.get_selected()][0]
        if kind == "mouse":
            a = Action.mouse(self.mouse_row.get_selected() + 1)
        elif kind == "key":
            if not self.action.key:
                self.action.key = keys.hid_for(keys.KEY_NAMES[self.key_list_row.get_selected()])
            a = Action("key", modifiers=self.action.modifiers, key=self.action.key)
        elif kind == "consumer":
            a = Action.consumer(keys.CONSUMER_NAMES[self.consumer_row.get_selected()])
        elif kind == "special":
            code = self.special_codes[self.special_row.get_selected()]
            a = Action.special(code, int(self.profile_row.get_value()) if code == 0x0D else 0)
        elif kind == "macro":
            if not self.macros:
                return
            a = Action.run_macro(self.macros[self.macro_row.get_selected()])
        else:
            a = Action(kind)
        self.on_done(a)
        self.close()


class MacroDialog(Adw.Dialog):
    """Edit a macro as text, or record it by typing into the dialog."""

    def __init__(self, macro: Macro | None, existing: list[str], on_done):
        super().__init__(title="Macro", content_width=560, content_height=520)
        self.on_done = on_done
        self.existing = existing
        self.original_name = macro.name if macro else None
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.ok = Gtk.Button(label="Save")
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", self._ok)
        header.pack_start(cancel)
        header.pack_end(self.ok)
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        page.add(group)
        self.name_row = Adw.EntryRow(title="Name", text=macro.name if macro else "")
        group.add(self.name_row)
        self.repeat_row = Adw.ComboRow(title="When the button is pressed", model=_string_list([REPEAT_LABELS[m] for m in REPEAT_MODES]))
        self.repeat_row.set_selected(REPEAT_MODES.index(macro.repeat) if macro else 0)
        group.add(self.repeat_row)

        steps_group = Adw.PreferencesGroup(
            title="Steps",
            description='Comma separated: key chords (Ctrl+C), delays (50ms), down:Shift / up:Shift, "text to type", mouse:1, consumer:Mute',
        )
        self.record = Gtk.ToggleButton(label="Record")
        self.record.set_valign(Gtk.Align.CENTER)
        self.record.connect("toggled", self._toggle_record)
        steps_group.set_header_suffix(self.record)
        self.text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True, top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        self.text.get_buffer().set_text(macro.to_text() if macro else "")
        self.text.get_buffer().connect("changed", lambda *_: self._validate())
        frame = Gtk.Frame()
        frame.set_child(self.text)
        frame.set_size_request(-1, 160)
        steps_group.add(frame)
        self.error = Gtk.Label(xalign=0, wrap=True)
        self.error.add_css_class("error")
        steps_group.add(self.error)
        self.delays = Adw.SwitchRow(title="Record delays between keys", active=True)
        steps_group.add(self.delays)
        page.add(steps_group)
        toolbar.set_content(page)
        self.set_child(toolbar)

        self.recorded: list[Step] = []
        self.last_time = 0.0
        ctl = Gtk.EventControllerKey()
        ctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctl.connect("key-pressed", self._key, True)
        ctl.connect("key-released", self._key_up)
        self.add_controller(ctl)
        self._validate()

    def _toggle_record(self, btn):
        if btn.get_active():
            self.recorded = []
            self.last_time = 0.0
            btn.set_label("Stop recording")
            self.text.set_sensitive(False)
        else:
            btn.set_label("Record")
            self.text.set_sensitive(True)
            if self.recorded:
                from ..macros import _merge_taps

                self.text.get_buffer().set_text(Macro("r", _merge_taps(self.recorded)).to_text())

    def _key(self, _ctl, _keyval, keycode, _state, down=True):
        if not self.record.get_active():
            return False
        name = keycode_to_name(keycode)
        if name is None:
            return True
        now = time.monotonic()
        if self.delays.get_active() and self.last_time:
            gap = int((now - self.last_time) * 1000)
            if gap >= 10:
                self.recorded.append(Step("delay", ms=gap))
        self.last_time = now
        self.recorded.append(Step("down" if down else "up", key=name))
        return True

    def _key_up(self, ctl, keyval, keycode, state):
        self._key(ctl, keyval, keycode, state, down=False)

    def _macro(self) -> Macro:
        buf = self.text.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        return Macro.parse(self.name_row.get_text().strip(), text, REPEAT_MODES[self.repeat_row.get_selected()])

    def _validate(self):
        try:
            m = self._macro()
            if not m.name:
                raise ValueError("give the macro a name")
            if m.name != self.original_name and m.name in self.existing:
                raise ValueError("a macro with this name already exists")
            if not m.steps:
                raise ValueError("add at least one step")
            self.error.set_text("")
            self.ok.set_sensitive(True)
        except ValueError as e:
            self.error.set_text(str(e))
            self.ok.set_sensitive(False)

    def _ok(self, *_):
        self.on_done(self.original_name, self._macro())
        self.close()


def confirm(parent, heading: str, body: str, action_label: str, on_yes, destructive: bool = False):
    d = Adw.AlertDialog(heading=heading, body=body)
    d.add_response("cancel", "Cancel")
    d.add_response("yes", action_label)
    d.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("cancel")
    d.connect("response", lambda _d, r: on_yes() if r == "yes" else None)
    d.present(parent)
