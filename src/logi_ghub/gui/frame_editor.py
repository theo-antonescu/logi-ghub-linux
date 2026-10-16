"""A frame-by-frame animation editor, like G HUB's "New Animation" screen.

Paint the keys of each frame, choose how long each frame shows and how it turns into the next,
and play it back. The result is a "frames" layer of a lighting design (see design.Layer).
"""

from __future__ import annotations

import copy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import layout  # noqa: E402
from ..design import TRANSITIONS, Design, Layer  # noqa: E402
from .keyboard import KeyboardView, int_to_rgba, rgba_to_int  # noqa: E402

DEFAULT_MS = 370


def _swatch(colour: int, size: int = 14) -> Gtk.Widget:
    box = Gtk.Box(width_request=size, height_request=size, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
    css = Gtk.CssProvider()
    css.load_from_string(f"box {{ background: #{colour:06x}; border-radius: {size // 2}px; }}")
    box.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    return box


class FrameEditor(Adw.Dialog):
    """``on_save(name, frames, cycle)`` is called with the edited frames."""

    def __init__(self, name: str, frames: list[dict], cycle: str, zones: list[int] | None, on_save):
        super().__init__(title="Animation", content_width=1180, content_height=720)
        self.on_save = on_save
        self.frames = copy.deepcopy(frames) or [{"colors": {}, "ms": DEFAULT_MS, "transition": "linear"}]
        self.cycle = cycle
        self.current = 0
        self.colour = 0x00FFFF
        self.erasing = False
        self.playing = None
        self.zones = zones
        self._names = {c.zone: c.name for c in layout.keycaps(zones)}
        self._zone_of = {n: z for z, n in self._names.items()}

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        header.pack_start(cancel)
        header.pack_end(save)
        toolbar.add_top_bar(header)

        body = Gtk.Box(spacing=24, margin_top=12, margin_bottom=12, margin_start=18, margin_end=18)
        toolbar.set_content(body)
        self.set_child(toolbar)

        # -- left: tools --------------------------------------------------------------------
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, width_request=300)
        body.append(left)
        self.name_row = Adw.EntryRow(title="Name", text=name)
        names = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        names.append(self.name_row)
        left.append(names)

        left.append(self._label("Colour"))
        row = Gtk.Box(spacing=8)
        self.picker = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False))
        self.picker.set_rgba(int_to_rgba(self.colour))
        self.picker.connect("notify::rgba", lambda b, _p: self._pick(rgba_to_int(b.get_rgba())))
        row.append(self.picker)
        self.eraser = Gtk.ToggleButton(label="Eraser", tooltip_text="Click keys to turn them off in this frame")
        self.eraser.connect("toggled", lambda b: setattr(self, "erasing", b.get_active()))
        row.append(self.eraser)
        fill = Gtk.Button(label="Fill", tooltip_text="Paint every key of this frame")
        fill.connect("clicked", lambda *_: self._fill())
        row.append(fill)
        clear = Gtk.Button(label="Clear", tooltip_text="Turn every key of this frame off")
        clear.connect("clicked", lambda *_: self._clear())
        row.append(clear)
        left.append(row)

        left.append(self._label("Colours in this animation"))
        self.used = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=10, row_spacing=4,
                                column_spacing=4)
        left.append(self.used)

        left.append(self._label("Change to the next frame"))
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3, row_spacing=4, column_spacing=4,
                           homogeneous=True)
        self.transition_buttons = {}
        first = None
        for key, (label, _f) in TRANSITIONS.items():
            b = Gtk.ToggleButton(label=label, group=first)
            first = first or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._set_transition(k))
            self.transition_buttons[key] = b
            grid.append(b)
        left.append(grid)

        left.append(self._label("When it reaches the end"))
        self.cycle_drop = Gtk.DropDown.new_from_strings(["Start again", "Play back and forth"])
        self.cycle_drop.set_selected(1 if cycle == "bounce" else 0)
        self.cycle_drop.connect("notify::selected", lambda d, _p: setattr(self, "cycle", "bounce" if d.get_selected() else "loop"))
        left.append(self.cycle_drop)

        left.append(self._label("This frame shows for"))
        speed = Gtk.Box(spacing=8)
        self.ms = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 40, 3000, 10)
        self.ms.set_hexpand(True)
        self.ms.connect("value-changed", lambda s: self._set_ms(int(s.get_value())))
        self.ms_label = Gtk.Label(width_chars=7, xalign=1)
        speed.append(self.ms)
        speed.append(self.ms_label)
        left.append(speed)
        all_ms = Gtk.Button(label="Use for every frame", halign=Gtk.Align.START)
        all_ms.connect("clicked", lambda *_: self._ms_for_all())
        left.append(all_ms)

        # -- right: keyboard and timeline ---------------------------------------------------------
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        body.append(right)
        self.kb = KeyboardView(zones, self._paint)
        right.append(self.kb)
        self.hint = Gtk.Label(label="Paint keys in the frame selected below.", css_classes=["dim-label"])
        right.append(self.hint)

        line = Gtk.Box(spacing=8)
        self.play = Gtk.ToggleButton(icon_name="media-playback-start-symbolic", tooltip_text="Play")
        self.play.connect("toggled", lambda b: self._play(b.get_active()))
        line.append(self.play)
        scroller = Gtk.ScrolledWindow(hexpand=True, vscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=70)
        self.strip = Gtk.Box(spacing=6)
        scroller.set_child(self.strip)
        line.append(scroller)
        for icon, tip, fn in (("list-add-symbolic", "Add a frame (a copy of this one)", self._add),
                              ("go-previous-symbolic", "Move this frame earlier", lambda: self._move(-1)),
                              ("go-next-symbolic", "Move this frame later", lambda: self._move(1)),
                              ("user-trash-symbolic", "Delete this frame", self._delete)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, f=fn: f())
            line.append(b)
        right.append(line)
        self.total = Gtk.Label(xalign=1, css_classes=["dim-label"])
        right.append(self.total)

        self.connect("closed", lambda *_: self._play(False))
        self._select(0)

    # -- helpers ----------------------------------------------------------------------------------
    @staticmethod
    def _label(text: str) -> Gtk.Label:
        return Gtk.Label(label=text, xalign=0, css_classes=["heading"])

    def _frame(self) -> dict:
        return self.frames[self.current]

    def _pick(self, colour: int):
        self.colour = colour
        self.eraser.set_active(False)

    def _show_frame(self):
        colours = self._frame()["colors"]
        self.kb.set_colors({self._zone_of[n]: c for n, c in colours.items() if n in self._zone_of})

    def _refresh(self):
        self._show_frame()
        f = self._frame()
        self.transition_buttons.get(f.get("transition", "linear"), self.transition_buttons["linear"]).set_active(True)
        self.ms.set_value(f.get("ms", DEFAULT_MS))
        self.ms_label.set_label(f"{f.get('ms', DEFAULT_MS)} ms")
        self._fill_strip()
        self._fill_used()
        total = sum(fr.get("ms", DEFAULT_MS) for fr in self.frames)
        self.total.set_label(f"{len(self.frames)} frame{'s' if len(self.frames) != 1 else ''} · {total / 1000:.2f} s per loop")

    def _fill_strip(self):
        while (c := self.strip.get_first_child()) is not None:
            self.strip.remove(c)
        for i, f in enumerate(self.frames):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            box.append(Gtk.Label(label=f"{i + 1}  ·  {f.get('ms', DEFAULT_MS)} ms"))
            dots = Gtk.Box(spacing=2, halign=Gtk.Align.CENTER)
            for c in list(dict.fromkeys(f["colors"].values()))[:8] or [0x202020]:
                dots.append(_swatch(c, 10))
            box.append(dots)
            b = Gtk.ToggleButton(child=box, active=i == self.current, width_request=96)
            b.connect("clicked", lambda _b, k=i: self._select(k))
            self.strip.append(b)

    def _fill_used(self):
        while (c := self.used.get_first_child()) is not None:
            self.used.remove(c)
        seen = list(dict.fromkeys(c for f in self.frames for c in f["colors"].values()))[:20]
        for c in seen:
            b = Gtk.Button(child=_swatch(c, 16), css_classes=["flat"], tooltip_text=f"#{c:06x}")
            b.connect("clicked", lambda _b, col=c: (self.picker.set_rgba(int_to_rgba(col)), self._pick(col)))
            self.used.append(b)

    # -- editing ------------------------------------------------------------------------------------
    def _select(self, i: int):
        self.current = max(0, min(i, len(self.frames) - 1))
        if self.play.get_active():
            self.play.set_active(False)
        self._refresh()

    def _paint(self, zone: int):
        if self.play.get_active():
            self.play.set_active(False)
        name = self._names.get(zone)
        if not name:
            return
        colours = self._frame()["colors"]
        if self.erasing:
            colours.pop(name, None)
        else:
            colours[name] = self.colour
        self._show_frame()
        self._fill_strip()
        self._fill_used()

    def _fill(self):
        self._frame()["colors"] = {n: self.colour for n in self._zone_of}
        self._refresh()

    def _clear(self):
        self._frame()["colors"] = {}
        self._refresh()

    def _set_transition(self, key: str):
        self._frame()["transition"] = key

    def _set_ms(self, ms: int):
        self._frame()["ms"] = ms
        self.ms_label.set_label(f"{ms} ms")
        total = sum(fr.get("ms", DEFAULT_MS) for fr in self.frames)
        self.total.set_label(f"{len(self.frames)} frame{'s' if len(self.frames) != 1 else ''} · {total / 1000:.2f} s per loop")

    def _ms_for_all(self):
        ms = self._frame().get("ms", DEFAULT_MS)
        for f in self.frames:
            f["ms"] = ms
        self._refresh()

    def _add(self):
        self.frames.insert(self.current + 1, copy.deepcopy(self._frame()))
        self._select(self.current + 1)

    def _delete(self):
        if len(self.frames) == 1:
            self._clear()
            return
        del self.frames[self.current]
        self._select(min(self.current, len(self.frames) - 1))

    def _move(self, step: int):
        j = self.current + step
        if 0 <= j < len(self.frames):
            self.frames[self.current], self.frames[j] = self.frames[j], self.frames[self.current]
            self._select(j)

    # -- playback ---------------------------------------------------------------------------------
    def _layer(self) -> Layer:
        return Layer("Frames", [], "frames", frames=copy.deepcopy(self.frames), cycle=self.cycle)

    def _play(self, on: bool):
        if self.playing:
            GLib.source_remove(self.playing)
            self.playing = None
        self.play.set_icon_name("media-playback-stop-symbolic" if on else "media-playback-start-symbolic")
        self.hint.set_label("Playing: press stop or click a frame to edit." if on else "Paint keys in the frame selected below.")
        if not on:
            self._show_frame()
            return
        r = Design("preview", [self._layer()]).renderer(self.zones)
        t0 = GLib.get_monotonic_time() / 1e6

        def tick():
            self.kb.set_colors(r.frame(GLib.get_monotonic_time() / 1e6 - t0))
            return True

        tick()
        self.playing = GLib.timeout_add(40, tick)

    def _save(self, *_):
        self._play(False)
        self.on_save(self.name_row.get_text().strip() or "Animation", copy.deepcopy(self.frames), self.cycle)
        self.close()
