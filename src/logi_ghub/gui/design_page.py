"""The Lighting page for keyboards with per-key lighting: one design, one picture, explicit Try and Save.

Mixed into DevicePane. The picture always says what it shows; editing changes only the picture
and the profile until you press Try on keyboard; Save to brightness + 9 stores exactly the frames
the picture plays.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import layout, lightfx, presets  # noqa: E402
from ..design import DIRECTIONS, EFFECTS, Design, Layer, fit_best, from_lighting  # noqa: E402
from .editors import confirm  # noqa: E402
from .keyboard import KeyboardView, int_to_rgba, rgba_to_int  # noqa: E402

FRAME_MS = 48  # what the keyboard plays a stored design at (slower only if it doesn't fit)


def _colour_button(value: int, tooltip: str, on_change) -> Gtk.ColorDialogButton:
    b = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER, tooltip_text=tooltip)
    b.set_rgba(int_to_rgba(value))
    b.connect("notify::rgba", lambda btn, _p: on_change(rgba_to_int(btn.get_rgba())))
    return b


class DesignLighting:
    """Methods for DevicePane (self.dev, self.profile, self.add_group, self.toast, self.fail, run_async...)."""

    # -- state ------------------------------------------------------------------------------
    def _design_init(self):
        light = self.profile.lighting
        if "design" not in light:
            legacy = light.get("keys") or (light.get("animation") or {}).get("enabled")
            light["design"] = (from_lighting(light, self.profile.name) if legacy else Design(self.profile.name)).to_dict()
        self.design = Design.from_dict(light["design"])
        self.picture = ("design", None)  # what the picture shows: design / stored slot / on-board profile
        self.keys_layer: int | None = None  # layer whose keys a click adds or removes
        self.open_layer = getattr(self, "open_layer", None)
        self.trying = getattr(self, "trying", False)
        # the keyboard may still show lighting this app sent earlier (an earlier session, say)
        self.leftover = self.light_state == "app" and not self.trying
        self._names = {c.zone: c.name for c in layout.keycaps(self.dev.per_key.zones)}

    def _design_changed(self, layers_changed: bool = False):
        self.profile.lighting["design"] = self.design.to_dict()
        for legacy in ("keys", "animation"):
            self.profile.lighting.pop(legacy, None)
        self.mark_dirty()
        if layers_changed:
            self._fill_layers()
        self._show_design()
        if self.trying:
            self._schedule_try()

    # -- page --------------------------------------------------------------------------------
    def run_async(self, fn, done=None):
        from .app import run_async

        run_async(fn, done, self.fail)

    def build_design_lighting(self):
        self._design_init()
        dev = self.dev

        # 1. the picture, with what it shows and the two things you can do with it
        g = self.add_group("lighting", Adw.PreferencesGroup())
        self.caption = Gtk.Label(xalign=0, wrap=True, css_classes=["heading"])
        self.caption_note = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        head.append(self.caption)
        head.append(self.caption_note)
        g.add(head)
        self.kb_view = KeyboardView(dev.per_key.zones, self._key_clicked)
        g.add(self.kb_view)
        bar = Gtk.Box(spacing=6, margin_top=6)
        self.back_btn = Gtk.Button(label="Back to my design")
        self.back_btn.connect("clicked", lambda *_: self._show_design())
        self.try_btn = Gtk.Button(label="Try on keyboard",
                                  tooltip_text="Play this design on the keyboard now. Nothing is saved to the keyboard.")
        self.try_btn.connect("clicked", lambda *_: self._toggle_try())
        save = Gtk.Button(label="Save to brightness + 9",
                          tooltip_text="Store this design in the keyboard; it plays with brightness + 9, even without this app")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save_to_slot())
        spacer = Gtk.Box(hexpand=True)
        bar.append(self.back_btn)
        bar.append(spacer)
        if self.light_state == "service":
            bar.append(Gtk.Label(label="Host mode is running this profile: Save applies changes", css_classes=["dim-label"]))
        else:
            self.try_btn.set_label("Stop trying" if self.trying else "Try on keyboard")
            bar.append(self.try_btn)
            if self.leftover:
                give = Gtk.Button(label="Give keyboard back",
                                  tooltip_text="The keyboard still shows lighting this app sent earlier; let it show its own again")
                give.connect("clicked", lambda *_: self._stop_trying())
                bar.append(give)
        bar.append(save)
        g.add(bar)

        # 2. the design's layers
        g = self.add_group("lighting", Adw.PreferencesGroup(
            title="Layers",
            description="Each layer gives some keys one effect; lower layers cover the ones above them. "
                        "Press Keys on a layer, then click keys in the picture to add or remove them."))
        hb = Gtk.Box(spacing=6)
        choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        popover = Gtk.Popover(child=choices)
        for key, title in [(k, t) for k, (t, _f) in presets.DESIGNS.items()] + [("empty", "Empty")]:
            b = Gtk.Button(label=title, css_classes=["flat"])
            b.connect("clicked", lambda _b, k=key: (popover.popdown(), self._start_from(k)))
            choices.append(b)
        anim = Gtk.Button(label="New animation…", tooltip_text="Add a layer you paint frame by frame",
                          valign=Gtk.Align.CENTER)
        anim.connect("clicked", lambda *_: self._new_animation())
        hb.append(anim)
        hb.append(Gtk.MenuButton(label="Start from…", popover=popover, valign=Gtk.Align.CENTER,
                                 tooltip_text="Replace the layers with a ready-made design"))
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a layer", valign=Gtk.Align.CENTER)
        add.connect("clicked", lambda *_: self._add_layer())
        hb.append(add)
        g.set_header_suffix(hb)
        self.layers_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        g.add(self.layers_box)
        self._fill_layers()

        # 3. brightness
        if dev.brightness:
            g = self.add_group("lighting", Adw.PreferencesGroup())
            row = Adw.ActionRow(title="Brightness", subtitle="Changes the keyboard right away")
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, dev.brightness.min, dev.brightness.max, 1)
            scale.set_hexpand(True)
            scale.set_size_request(240, -1)
            scale.set_value(self.device_brightness if self.device_brightness is not None else dev.brightness.max)
            scale.connect("value-changed", lambda s: self.run_async(lambda: dev.brightness.set(int(s.get_value()))))
            row.add_suffix(scale)
            g.add(row)

        # 4. what is stored on the keyboard
        self._on_keyboard_group()
        self._show_design()

    # -- layers list ---------------------------------------------------------------------------
    def _fill_layers(self):
        box = self.layers_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        if not self.design.layers:
            box.append(Adw.ActionRow(title="No layers yet", subtitle="Add one with +, or start from a ready-made design"))
        for i, layer in enumerate(self.design.layers):
            box.append(self._layer_row(i, layer))
        self._update_selection()

    def _layer_row(self, i: int, layer: Layer) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow(title=GLib.markup_escape_text(layer.name), subtitle=self._layer_summary(layer))
        row.set_expanded(self.open_layer == i)
        row.connect("notify::expanded", lambda r, _p: setattr(self, "open_layer", i if r.get_expanded() else
                                                               (None if self.open_layer == i else self.open_layer)))
        swatch = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
        for c in (layer.colors if layer.effect != "rainbow" else [0xFF0000, 0x00FF00, 0x0000FF])[:4]:
            dot = Gtk.Box(width_request=12, height_request=12)
            dot.add_css_class("lgl-swatch")
            css = Gtk.CssProvider()
            css.load_from_string(f"box {{ background: #{c:06x}; border-radius: 6px; }}")
            dot.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            swatch.append(dot)
        row.add_prefix(swatch)

        for icon, tip, fn in (("user-trash-symbolic", "Delete this layer", lambda: self._delete_layer(i)),
                              ("go-down-symbolic", "Move down", lambda: self._move_layer(i, 1)),
                              ("go-up-symbolic", "Move up", lambda: self._move_layer(i, -1))):
            b = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=tip, css_classes=["flat"])
            b.connect("clicked", lambda _b, f=fn: f())
            row.add_suffix(b)
        keys_btn = Gtk.ToggleButton(label="Keys", valign=Gtk.Align.CENTER, active=self.keys_layer == i,
                                    tooltip_text="Click keys in the picture to add them to this layer or remove them")
        keys_btn.connect("toggled", lambda b: self._pick_keys(i if b.get_active() else None))
        row.add_suffix(keys_btn)

        name = Adw.EntryRow(title="Name", text=layer.name)
        name.connect("changed", lambda e: (setattr(layer, "name", e.get_text()), row.set_title(GLib.markup_escape_text(e.get_text())),
                                           self._design_changed()))
        row.add_row(name)

        effect_ids = list(EFFECTS)
        effect = Adw.ComboRow(title="Effect", model=Gtk.StringList.new(list(EFFECTS.values())))
        effect.set_selected(effect_ids.index(layer.effect) if layer.effect in effect_ids else 0)
        row.add_row(effect)

        colors_row = Adw.ActionRow(title="Colours", subtitle="In order")
        colors_box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        colors_row.add_suffix(colors_box)
        row.add_row(colors_row)

        speed = Adw.SpinRow.new_with_range(0.2, 60, 0.1)
        speed.set_title("Seconds per cycle")
        speed.set_digits(1)
        speed.set_value(layer.period_ms / 1000)
        row.add_row(speed)
        dir_ids = list(DIRECTIONS)
        direction = Adw.ComboRow(title="Direction", model=Gtk.StringList.new(list(DIRECTIONS.values())))
        direction.set_selected(dir_ids.index(layer.direction) if layer.direction in dir_ids else 0)
        row.add_row(direction)
        spread = Adw.SpinRow.new_with_range(0, 3, 0.05)
        spread.set_title("Wave length")
        spread.set_subtitle("How far apart in the cycle the first and last keys are")
        spread.set_digits(2)
        spread.set_value(layer.spread)
        row.add_row(spread)
        floor = Adw.SpinRow.new_with_range(0, 1, 0.05)
        floor.set_title("Dimmest level")
        floor.set_digits(2)
        floor.set_value(layer.floor)
        row.add_row(floor)
        fade = Adw.SwitchRow(title="Fade between colours", active=layer.fade)
        row.add_row(fade)
        bg_row = Adw.ActionRow(title="Background", subtitle="What the keys show where the effect isn't lit")
        bg_dark = Gtk.ToggleButton(label="Dark", valign=Gtk.Align.CENTER, active=layer.background is None)
        bg_btn = _colour_button(layer.background if layer.background is not None else 0x50B0FF, "Background colour",
                                lambda v: (setattr(layer, "background", v), bg_dark.set_active(False), changed()))
        bg_dark.connect("toggled", lambda b: (setattr(layer, "background", None if b.get_active() else rgba_to_int(bg_btn.get_rgba())),
                                              changed()))
        bg_row.add_suffix(bg_btn)
        bg_row.add_suffix(bg_dark)
        row.add_row(bg_row)
        frames_row = Adw.ActionRow(title="Frames")
        edit_frames = Gtk.Button(label="Edit frames…", valign=Gtk.Align.CENTER)
        edit_frames.add_css_class("suggested-action")
        edit_frames.connect("clicked", lambda *_: self._edit_frames(layer))
        frames_row.add_suffix(edit_frames)
        row.add_row(frames_row)

        def rebuild_colors():
            while (child := colors_box.get_first_child()) is not None:
                colors_box.remove(child)
            for k, c in enumerate(layer.colors):
                colors_box.append(_colour_button(c, f"Colour {k + 1}", lambda v, k=k: (layer.colors.__setitem__(k, v), changed())))
            if layer.effect not in ("static", "breathing", "rainbow"):
                if len(layer.colors) > 1:
                    rm = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove the last colour", css_classes=["flat"])
                    rm.connect("clicked", lambda *_: (layer.colors.pop(), rebuild_colors(), changed()))
                    colors_box.append(rm)
                if len(layer.colors) < 12:
                    plus = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a colour", css_classes=["flat"])
                    plus.connect("clicked", lambda *_: (layer.colors.append(layer.colors[-1]), rebuild_colors(), changed()))
                    colors_box.append(plus)

        def sync():
            e = layer.effect
            frames_row.set_visible(e == "frames")
            frames_row.set_subtitle(f"{len(layer.frames)} frame{'s' if len(layer.frames) != 1 else ''}, "
                                    f"{layer.cycle_ms / 1000:.2f} s per loop" if layer.frames else "No frames yet")
            keys_btn.set_visible(e != "frames")  # frames say for themselves which keys they light
            colors_row.set_visible(e not in ("rainbow", "frames"))
            speed.set_visible(e not in ("static", "frames"))
            bg_row.set_visible(e in ("raise", "sparkle"))
            spread.set_visible(e in ("comet", "sparkle") or (e in ("sequence", "ripple", "rainbow") and layer.direction != "none"))
            spread.set_title({"comet": "Tail length", "sparkle": "How many keys sparkle"}.get(e, "Wave length"))
            spread.set_subtitle({"comet": "Part of the circle the tail covers", "sparkle": "Share of the keys flashing at a time"}
                                .get(e, "How far apart in the cycle the first and last keys are"))
            floor.set_visible(e in ("ripple", "breathing", "comet", "sparkle", "heartbeat", "raise"))
            direction.set_visible(e not in ("static", "pinwheel", "comet", "turning", "frames"))
            fade.set_visible(e == "sequence")
            if e in ("static", "breathing") and len(layer.colors) > 1:
                del layer.colors[1:]
            rebuild_colors()

        def changed(*_):
            row.set_subtitle(self._layer_summary(layer))
            self._design_changed()

        def on_effect(*_):
            layer.effect = effect_ids[effect.get_selected()]
            sync()
            changed()

        def on_direction(*_):
            layer.direction = dir_ids[direction.get_selected()]
            sync()
            changed()

        effect.connect("notify::selected", on_effect)
        direction.connect("notify::selected", on_direction)
        speed.connect("notify::value", lambda s, _p: (setattr(layer, "period_ms", int(s.get_value() * 1000)), changed()))
        spread.connect("notify::value", lambda s, _p: (setattr(layer, "spread", s.get_value()), changed()))
        floor.connect("notify::value", lambda s, _p: (setattr(layer, "floor", s.get_value()), changed()))
        fade.connect("notify::active", lambda s, _p: (setattr(layer, "fade", s.get_active()), changed()))
        sync()
        return row

    @staticmethod
    def _layer_summary(layer: Layer) -> str:
        what = EFFECTS.get(layer.effect, layer.effect)
        if layer.effect == "frames":
            n = len(layer.frame_keys)
            return f"{what} · {len(layer.frames)} frames · {n} key{'s' if n != 1 else ''}"
        if layer.effect != "static":
            what += f", {layer.period_ms / 1000:g} s"
        return f"{what} · {len(layer.keys)} key{'s' if len(layer.keys) != 1 else ''}"

    def _edit_frames(self, layer: Layer):
        from .frame_editor import FrameEditor

        def saved(name, frames, cycle):
            layer.name, layer.frames, layer.cycle = name, frames, cycle
            self._design_changed(layers_changed=True)

        FrameEditor(layer.name, layer.frames, layer.cycle, self.dev.per_key.zones, saved).present(self.win)

    def _new_animation(self):
        """A layer you paint frame by frame, opened straight in the frame editor."""
        layer = Layer(f"Animation {len(self.design.layers) + 1}", [], "frames")
        self.design.layers.append(layer)
        self.open_layer = len(self.design.layers) - 1
        self.keys_layer = None
        self._design_changed(layers_changed=True)
        self._edit_frames(layer)

    def _add_layer(self):
        self.design.layers.append(Layer(f"Layer {len(self.design.layers) + 1}", [], "static", [self.paint_color]))
        i = len(self.design.layers) - 1
        self.open_layer = i
        self.keys_layer = i
        self._design_changed(layers_changed=True)
        self.toast("Click keys in the picture to add them to the new layer")

    def _delete_layer(self, i: int):
        del self.design.layers[i]
        self.keys_layer = self.open_layer = None
        self._design_changed(layers_changed=True)

    def _move_layer(self, i: int, step: int):
        j = i + step
        if 0 <= j < len(self.design.layers):
            layers = self.design.layers
            layers[i], layers[j] = layers[j], layers[i]
            self.open_layer = j if self.open_layer == i else self.open_layer
            self.keys_layer = j if self.keys_layer == i else self.keys_layer
            self._design_changed(layers_changed=True)

    def _start_from(self, key: str):
        def go():
            if key == "empty":
                self.design = Design(self.profile.name)
            else:
                self.design = presets.DESIGNS[key][1]()
            self.keys_layer = self.open_layer = None
            self._design_changed(layers_changed=True)

        if self.design.layers:
            title = "an empty design" if key == "empty" else f"“{presets.DESIGNS[key][0]}”"
            confirm(self.win, "Replace the layers?", f"This replaces this profile's layers with {title}.", "Replace", go)
        else:
            go()

    # -- picking keys -----------------------------------------------------------------------------
    def _pick_keys(self, i: int | None):
        if self.keys_layer == i:
            return
        self.keys_layer = i
        self._fill_layers()  # only one Keys button can be on
        if i is not None and self.picture[0] != "design":
            self._show_design()

    def _update_selection(self):
        if self.keys_layer is not None and self.keys_layer < len(self.design.layers):
            names = set(self.design.layers[self.keys_layer].keys)
            self.kb_view.set_selected(z for z, n in self._names.items() if n in names)
        else:
            self.kb_view.set_selected(())

    def _key_clicked(self, zone: int):
        if self.keys_layer is None or self.keys_layer >= len(self.design.layers):
            if self.design.layers:
                self.toast("Press Keys on a layer first, then click keys to add or remove them")
            else:
                self._add_layer()
                self._key_clicked(zone)
            return
        name = self._names.get(zone)
        layer = self.design.layers[self.keys_layer]
        if name in layer.keys:
            layer.keys.remove(name)
        elif name:
            layer.keys.append(name)
        row = self.layers_box.get_row_at_index(self.keys_layer)
        if isinstance(row, Adw.ExpanderRow):
            row.set_subtitle(self._layer_summary(layer))
        self._update_selection()
        self._design_changed()

    # -- the picture --------------------------------------------------------------------------------
    def _set_caption(self, title: str, note: str):
        self.caption.set_label(title)
        self.caption_note.set_label(note)
        self.caption_note.set_visible(bool(note))

    def _play(self, frame_at, period_ms: int, animated: bool):
        """Show frame_at(t) in the picture, stepping every period_ms like the keyboard does."""
        self._stop_preview()
        self._t0 = GLib.get_monotonic_time() / 1e6

        def tick():
            t = GLib.get_monotonic_time() / 1e6 - self._t0
            self.kb_view.set_colors(frame_at(int(t * 1000 / period_ms) * period_ms / 1000))
            return True

        tick()
        if animated:
            self._preview_source = GLib.timeout_add(period_ms, tick)

    def _show_design(self):
        self.picture = ("design", None)
        self.back_btn.set_visible(False)
        r = self.design.renderer(self.dev.per_key.zones)
        self._play(r.frame, FRAME_MS, self.design.animated)
        self._update_selection()
        if self.trying:
            title, note = f"Your design “{self.design.name}”, on the keyboard now", "Press Stop trying to give the keyboard back."
        elif self.light_state == "service":
            title, note = f"Your design “{self.design.name}”", "Host mode is running this profile; it shows on the keyboard when you save."
        else:
            title, note = f"Your design “{self.design.name}” (not on the keyboard)", (
                "The keyboard keeps showing its own lighting until you press Try on keyboard or Save to brightness + 9.")
        if self.leftover and not self.trying:
            note = "The keyboard is still showing lighting this app sent earlier. Give keyboard back to end that."
        if not self.design.layers:
            note = "Add a layer, or start from a ready-made design."
        self._set_caption(title, note)

    def _show_stored_slot(self, slot: int, info: dict):
        self.picture = ("slot", slot)
        self.back_btn.set_visible(True)
        frames, period = info["frames"], info["period_ms"]
        total = sum(d for _, d in frames) or 1

        def at(t):
            pos = int(t * 1000 / max(1, period)) % total
            for colors, dur in frames:
                if pos < dur:
                    return colors
                pos -= dur
            return frames[-1][0]

        self._play(at, max(16, period), len(frames) > 1)
        self.kb_view.set_selected(())
        self._set_caption(f"Brightness + {8 + slot}: “{info['name']}”, stored on the keyboard",
                          "Exactly what the keyboard plays (from this computer's copy).")

    def _show_onboard_profile(self, number: int, light: dict):
        self.picture = ("profile", number)
        self.back_btn.set_visible(True)
        r = lightfx.Renderer(light, self.dev.per_key.zones, len(self.dev.rgb.zones) if self.dev.rgb else 1)
        self._play(r.frame, 66, r.is_animated())
        self.kb_view.set_selected(())
        self._set_caption(f"On-board profile {number}",
                          "Approximate: the keyboard draws its built-in effects itself, and this picture only imitates them.")

    # -- trying and saving ----------------------------------------------------------------------------
    def _toggle_try(self):
        if self.trying:
            self._stop_trying()
        else:
            self.trying = True
            self.try_btn.set_label("Stop trying")
            self.light_state = "app"
            self._schedule_try()
            self._show_design()

    def _schedule_try(self):
        if getattr(self, "_apply_pending", None):
            GLib.source_remove(self._apply_pending)
        self._apply_pending = GLib.timeout_add(200, self._try_now)

    def _try_now(self):
        self._apply_pending = None
        light = {"design": self.design.to_dict()}
        dev = self.dev

        def work():
            old = getattr(self, "animator", None)
            if old:
                old.stop()
                old.join(timeout=1)
            anim = lightfx.apply(dev, light, claim=not getattr(self, "_claimed", False))
            self._claimed = True
            return anim

        self.run_async(work, lambda anim: setattr(self, "animator", anim))
        return False

    def _stop_trying(self):
        self.trying = False
        self.leftover = False
        self.try_btn.set_label("Try on keyboard")
        self.release_lighting(note="Press brightness + 8 or + 9 to bring back a custom animation.")

    def _save_to_slot(self):
        from ..features import rgb_store

        current = self.onboard_effects[1] if len(self.onboard_effects) > 1 else "the current animation"
        d = Design.from_dict(self.design.to_dict())
        if not d.layers:
            self.toast("The design is empty: add a layer first")
            return

        def go():
            self.toast("Saving to the keyboard…")

            def work():
                frames, period, data, _detail = fit_best(d, self.dev.per_key.zones)
                anim = getattr(self, "animator", None)
                if anim:
                    anim.stop()
                rgb_store.Uploader(self.dev.hidpp).upload(1, data)
                rgb_store.save_local(self.dev.key, 1, data)
                return period, rgb_store.parse_file(data), self.dev.rgb.onboard_effect_names()

            def done(r):
                period, info, names = r
                self.onboard_effects = names
                slower = f" It plays at {period} ms per frame so it fits." if period != FRAME_MS else ""
                self.toast(f"Saved “{info['name']}”. Press brightness + 9 to see it.{slower}")
                self.build_lighting()

            self.run_async(work, done)

        confirm(self.win, "Save to brightness + 9?",
                f"This replaces “{current}” on the keyboard with “{d.name}”. Brightness + 8 is never changed.", "Save", go)

    # -- what the keyboard has stored ------------------------------------------------------------------------
    def _on_keyboard_group(self):
        from ..features import rgb_store

        if not (self.stored_profiles or self.onboard_effects):
            return
        if self.stored_profiles:
            self._fallback_group()
        g = self.add_group("lighting", Adw.PreferencesGroup(
            title="Stored on the keyboard",
            description="What the keyboard plays by itself. Fn + F2/F3/F4 switch its profiles; brightness + 8 and + 9 "
                        "play its two custom animations."))
        locals_ = {slot: rgb_store.load_local(self.dev.key, slot) for slot in range(2)}
        for slot, name in enumerate(self.onboard_effects[:2]):
            local = locals_[slot]
            backed = local is not None and local["name"] == name
            sub = f"“{name}”" + (" · protected" if slot == 0 else "") + ("" if backed else " · no copy on this computer to preview")
            row = Adw.ActionRow(title=f"Brightness + {8 + slot}", subtitle=GLib.markup_escape_text(sub))
            if backed:
                b = Gtk.Button(label="Preview", valign=Gtk.Align.CENTER)
                b.connect("clicked", lambda _b, s=slot, info=local: self._show_stored_slot(s, info))
                row.add_suffix(b)
            if slot == 1 and backed:
                b = Gtk.Button(label="Edit as design", valign=Gtk.Align.CENTER,
                               tooltip_text="Open the ready-made design this was made from, if there is one")
                key = next((k for k, (t, _f) in presets.DESIGNS.items() if t == name), None)
                b.set_sensitive(key is not None)
                b.connect("clicked", lambda _b, k=key: k and self._start_from(k))
                row.add_suffix(b)
            g.add(row)
        for sp in self.stored_profiles:
            light = sp.lighting
            title = f"Profile {sp.number}" + (f": {sp.name}" if sp.name and not sp.name.startswith("PROFILE_NAM") else "")
            desc = light.describe()
            slot = light.params[0] if light.effect_id == 0x0C else None
            if slot is not None and slot < len(self.onboard_effects):
                desc = f"Custom animation “{self.onboard_effects[slot]}”"
            if sp.number == self.stored_active:
                desc += " · in use"
            row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(desc))
            if sp.number == self.stored_active:
                row.add_prefix(Gtk.Image(icon_name="object-select-symbolic", tooltip_text="The profile the keyboard is using"))
            if slot is not None:
                local = locals_.get(slot)
                if local is not None and slot < len(self.onboard_effects) and local["name"] == self.onboard_effects[slot]:
                    b = Gtk.Button(label="Preview", valign=Gtk.Align.CENTER)
                    b.connect("clicked", lambda _b, s=slot, info=local: self._show_stored_slot(s, info))
                    row.add_suffix(b)
            elif (pv := light.to_lighting()) is not None:
                b = Gtk.Button(label="Preview", valign=Gtk.Align.CENTER, tooltip_text="Approximate")
                b.connect("clicked", lambda _b, n=sp.number, v=pv: self._show_onboard_profile(n, v))
                row.add_suffix(b)
            g.add(row)

    def _profile_label(self, sp) -> str:
        light = sp.lighting
        slot = light.params[0] if light.effect_id == 0x0C else None
        if slot is not None and slot < len(self.onboard_effects):
            what = f"“{self.onboard_effects[slot]}” animation"
        else:
            what = light.describe()
        return f"Profile {sp.number}: {what}"

    def _fallback_group(self):
        """Which on-board profile the keyboard falls back to (the active one)."""
        from ..features.profile_fs import ProfileFS

        g = self.add_group("lighting", Adw.PreferencesGroup(
            title="Fallback lighting",
            description="What the keyboard shows when nothing else is chosen: after this app or G HUB lets go, after a "
                        "restart, or until you press brightness + 8 or + 9. Same as Fn + F2/F3/F4."))
        numbers = [sp.number for sp in self.stored_profiles]
        combo = Adw.ComboRow(title="Fall back to", model=Gtk.StringList.new([self._profile_label(sp) for sp in self.stored_profiles]))
        if self.stored_active in numbers:
            combo.set_selected(numbers.index(self.stored_active))

        def picked(*_):
            n = numbers[combo.get_selected()]
            if n == self.stored_active:
                return
            self.stored_active = n
            self.run_async(lambda: ProfileFS(self.dev.hidpp).select(n),
                           lambda _r: self.toast(f"The keyboard now falls back to profile {n}"))

        combo.connect("notify::selected", picked)
        g.add(combo)

