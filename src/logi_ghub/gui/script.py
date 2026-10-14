"""The Script page: a computer profile's Lua script (G HUB's Lua API), its output, and a dry run."""

from __future__ import annotations

import os

from gi.repository import Adw, Gio, GLib, Gtk

from .. import profiles, scripting

DOCS_URL = "https://github.com/theo-antonescu/logi-ghub-linux/blob/main/docs/lua-scripting.md"
TRY_DEFAULT = "PROFILE_ACTIVATED, MOUSE_BUTTON_PRESSED:4, MOUSE_BUTTON_RELEASED:4@400"


def _code_view(editable: bool, height: int) -> tuple[Gtk.Widget, Gtk.TextView]:
    view = Gtk.TextView(monospace=True, editable=editable, cursor_visible=editable, wrap_mode=Gtk.WrapMode.NONE,
                        top_margin=8, bottom_margin=8, left_margin=10, right_margin=10)
    scroller = Gtk.ScrolledWindow(child=view, min_content_height=height, hexpand=True)
    frame = Gtk.Frame(child=scroller, margin_top=6)
    return frame, view


def _scroll_to_end(view: Gtk.TextView) -> None:
    buf = view.get_buffer()
    view.scroll_to_mark(buf.create_mark(None, buf.get_end_iter(), False), 0.0, False, 0, 1)


class ScriptPage:
    """Builds the page for one device pane; rebuilt whenever the pane switches profiles."""

    def __init__(self, pane, daemon_running):
        self.pane = pane
        self.daemon_running = daemon_running
        self.timer = 0
        self.check_timer = 0
        self.log_mtime = None
        self.was_shown = False

    def stop(self) -> None:
        for attr in ("timer", "check_timer"):
            if getattr(self, attr):
                GLib.source_remove(getattr(self, attr))
                setattr(self, attr, 0)

    def build(self) -> None:
        pane, dev, p = self.pane, self.pane.dev, self.pane.profile
        self.stop()
        pane.page_reset("script")
        visible = bool(p and dev.hidpp is not None)  # host mode needs HID++ (not the G600)
        pane.stack.get_page(pane.pages["script"]).set_visible(visible)
        if not visible:
            return
        g = pane.add_group("script", Adw.PreferencesGroup(
            title="Lua script",
            description="Like G HUB's scripting: OnEvent(event, arg, family) runs on this computer while the profile is "
                        "active in host mode. Scripts can press keys and mouse buttons, move the pointer, play macros, "
                        "change DPI and lighting, but can't open files or run programs."))
        g.set_header_suffix(Gtk.LinkButton(uri=DOCS_URL, label="Lua reference", valign=Gtk.Align.CENTER))
        if pane.target is None or pane.target[0] != "computer":
            row = Adw.ActionRow(title="Scripts belong to profiles on this computer",
                                subtitle="On-board slots can't hold scripts. Use “Save current as…” on the Profiles "
                                         "page, then add a script to that profile.")
            row.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
            g.add(row)
            return
        if not scripting.available():
            row = Adw.ActionRow(title="Lua is not installed", subtitle=GLib.markup_escape_text(scripting.MISSING))
            row.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
            g.add(row)

        g.add(self._state_row(p))
        enabled = Adw.SwitchRow(title="Run the script", subtitle="Off keeps the script but doesn't run it",
                                active=p.script_enabled)
        enabled.connect("notify::active", lambda r, _p: self._set_enabled(r.get_active()))
        g.add(enabled)

        frame, self.editor = _code_view(True, 340)
        buf = self.editor.get_buffer()
        buf.set_text(p.script or "")
        buf.connect("changed", self._edited)
        g.add(frame)
        self.status = Gtk.Label(xalign=0, wrap=True, margin_top=6, css_classes=["dim-label"])
        g.add(self.status)
        tools = Gtk.Box(spacing=6, margin_top=6)
        for label, fn in (("Insert example", self._insert_example), ("Open file…", self._open_file)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda *_a, f=fn: f())
            tools.append(b)
        g.add(tools)
        self._check_now()

        t = pane.add_group("script", Adw.PreferencesGroup(
            title="Try it", description="Feeds events to the script on a simulated clock and lists what it would do. "
                                        "Nothing is typed or clicked. Format: EVENT:ARG@milliseconds."))
        self.try_row = Adw.EntryRow(title="Events", text=TRY_DEFAULT, show_apply_button=True)
        self.try_row.connect("apply", lambda *_: self._try())
        t.add(self.try_row)
        run = Adw.ButtonRow(title="Run")
        run.connect("activated", lambda *_: self._try())
        t.add(run)
        self.try_frame, self.try_view = _code_view(False, 160)
        self.try_frame.set_visible(False)
        t.add(self.try_frame)

        o = pane.add_group("script", Adw.PreferencesGroup(
            title="Output", description="What the running script printed with OutputLogMessage, and its errors."))
        clear = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear.connect("clicked", lambda *_: self._clear_log())
        o.set_header_suffix(clear)
        log_frame, self.log_view = _code_view(False, 160)
        o.add(log_frame)
        self.log_mtime = None
        self._refresh_log()
        self.timer = GLib.timeout_add(1000, self._refresh_log)

        names = self._button_names()
        if names:
            r = pane.add_group("script", Adw.PreferencesGroup(title="Button numbers"))
            exp = Adw.ExpanderRow(title="What arg is for each button",
                                  subtitle="MOUSE_BUTTON_PRESSED / RELEASED. Left click (1) is only reported after "
                                           "EnablePrimaryMouseButtonEvents(true)")
            for n, name in names:
                exp.add_row(Adw.ActionRow(title=GLib.markup_escape_text(name), subtitle=f"arg = {n}"))
            r.add(exp)

    # -- pieces ----------------------------------------------------------------------------------
    def _state_row(self, p: profiles.Profile) -> Adw.ActionRow:
        active = profiles.get_active().get(self.pane.dev.key) == p.name
        if active and self.daemon_running():
            title, sub, icon = "Running", "This profile is active in host mode; saved changes restart the script.", "media-playback-start-symbolic"
        elif active:
            title, sub, icon = "Not running", "The logi-ghubd service isn't running: systemctl --user enable --now logi-ghubd", "dialog-warning-symbolic"
        else:
            title, sub, icon = "Not running", "Turn on Host mode for this profile on the Profiles page to run its script.", "media-playback-pause-symbolic"
        row = Adw.ActionRow(title=title, subtitle=sub)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        return row

    def _button_names(self) -> list[tuple[int, str]]:
        dev = self.pane.dev
        count = self.pane.info.get("spy_buttons", 0) if getattr(self.pane, "info", None) else 0
        if not count:
            return []
        nums = scripting.script_button_numbers([dev.button_name(i) for i in range(count)], count)
        return sorted((n, dev.button_name(i)) for i, n in nums.items())

    def _set_enabled(self, on: bool) -> None:
        if self.pane.profile.script_enabled != on:
            self.pane.profile.script_enabled = on
            self.pane.mark_dirty()

    def _edited(self, buf: Gtk.TextBuffer) -> None:
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if text != self.pane.profile.script:
            self.pane.profile.script = text
            self.pane.mark_dirty()
        if self.check_timer:
            GLib.source_remove(self.check_timer)
        self.check_timer = GLib.timeout_add(600, self._check_now)

    def _check_now(self) -> bool:
        self.check_timer = 0
        src = self.pane.profile.script
        if not src.strip():
            self.status.set_label("No script. Write one, insert the example, or open a .lua file.")
        elif not scripting.available():
            self.status.set_label("")
        else:
            err = scripting.check(src)
            self.status.set_label(f"⚠ {err}" if err else "✓ Compiles")
        return False

    def _insert_example(self) -> None:
        buf = self.editor.get_buffer()
        if self.pane.profile.script.strip():
            buf.insert(buf.get_end_iter(), "\n" + scripting.EXAMPLE)
        else:
            buf.set_text(scripting.EXAMPLE)

    def _open_file(self) -> None:
        dialog = Gtk.FileDialog(title="Open a Lua script")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter(name="Lua scripts")
        f.add_pattern("*.lua")
        f.add_pattern("*.txt")
        filters.append(f)
        dialog.set_filters(filters)

        def picked(d, result):
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return
            try:
                text = open(file.get_path(), encoding="utf-8", errors="replace").read()
            except OSError as e:
                self.pane.fail(e)
                return
            self.editor.get_buffer().set_text(text)

        dialog.open(self.pane.win, None, picked)

    def _try(self) -> None:
        from .app import run_async

        source = self.pane.profile.script
        try:
            specs = [s.strip() for s in self.try_row.get_text().split(",") if s.strip()]
            events = scripting.parse_events(specs)
        except ValueError as e:
            self.pane.toast(str(e))
            return

        def done(result):
            actions, version = result
            self.try_view.get_buffer().set_text(f"dry run ({version or 'Lua'}):\n" + "\n".join(actions))
            self.try_frame.set_visible(True)

        run_async(lambda: scripting.simulate(source, events), done, self.pane.fail)

    def _log_path(self):
        return scripting.log_path(self.pane.dev.key, self.pane.profile.name)

    def _refresh_log(self) -> bool:
        if self.pane.get_root() is None:
            if self.was_shown:  # the pane was closed (device gone, rescan)
                self.timer = 0
                return False
            return True
        self.was_shown = True
        if self.pane.stack.get_visible_child_name() != "script" and self.log_mtime is not None:
            return True
        path = self._log_path()
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = 0
        if mtime != self.log_mtime:
            self.log_mtime = mtime
            text = scripting.read_log(path)
            self.log_view.get_buffer().set_text(text or "(nothing yet)")
            _scroll_to_end(self.log_view)
        return True

    def _clear_log(self) -> None:
        try:
            self._log_path().write_text("")
        except OSError as e:
            self.pane.fail(e)
        self.log_mtime = None
        self._refresh_log()
