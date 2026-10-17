#!/usr/bin/env python3
"""GTK4 / libadwaita front-end for solocast2.py."""
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import solocast2  # noqa: E402

APP_ID = "io.github.raffaelbdl.SoloCast2"
DEBOUNCE_MS = 150


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="SoloCast 2", default_width=460, default_height=560)
        self.mic = None
        self.mixer = None
        self._loading = False

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Re-read settings from the mic")
        refresh.connect("clicked", lambda *_: self.connect_device())
        header = Adw.HeaderBar()
        header.pack_start(refresh)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.toasts)
        self.set_content(view)

        self.page = self._build_page()
        self.error = Adw.StatusPage(icon_name="audio-input-microphone-symbolic", title="SoloCast 2 not found")
        retry = Gtk.Button(label="Retry", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        retry.connect("clicked", lambda *_: self.connect_device())
        self.error.set_child(retry)

        self.connect_device()

    def _build_page(self):
        page = Adw.PreferencesPage()

        mic = Adw.PreferencesGroup(title="Microphone")
        self.gain = self._db_scale(self._apply_gain)
        self.gain.set_increments(0.5, 3)
        gain_row = Adw.ActionRow(title="Gain")
        gain_row.add_suffix(self.gain)
        mic.add(gain_row)
        self.mute = Adw.SwitchRow(title="Mute")
        self.mute.connect("notify::active", self._on_mute)
        mic.add(self.mute)
        page.add(mic)

        filters = Adw.PreferencesGroup(
            title="Frequency filter",
            description="Applied by the mic itself, so every application gets the filtered signal.",
        )
        self.high_pass = Adw.SwitchRow(title="High-pass filter", subtitle="Reduces low rumble (desk bumps, fans, traffic)")
        self.low_pass = Adw.SwitchRow(title="Low-pass filter", subtitle="Reduces high-frequency hiss")
        for row in (self.high_pass, self.low_pass):
            row.connect("notify::active", self._on_filter)
            filters.add(row)
        page.add(filters)

        boost = Adw.PreferencesGroup(
            title="Presence boost",
            description=f"Onboard EQ band around {solocast2.BOOST_FREQUENCY_HZ // 1000} kHz for vocal clarity. 0 dB is off.",
        )
        self.boost = self._db_scale(self._apply_boost)
        self.boost.set_range(solocast2.BOOST_MIN, solocast2.BOOST_MAX)
        self.boost.set_increments(solocast2.BOOST_STEP, 10 * solocast2.BOOST_STEP)
        self.boost.set_round_digits(1)
        for mark in range(int(solocast2.BOOST_MIN), int(solocast2.BOOST_MAX) + 1, 3):
            self.boost.add_mark(mark, Gtk.PositionType.BOTTOM, None)
        reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Reset to 0 dB")
        reset.add_css_class("flat")
        reset.connect("clicked", lambda *_: self.boost.set_value(0))
        boost_row = Adw.ActionRow(title="Level")
        boost_row.add_suffix(self.boost)
        boost_row.add_suffix(reset)
        boost.add(boost_row)
        page.add(boost)
        return page

    def _db_scale(self, apply):
        scale = Gtk.Scale(hexpand=True, draw_value=True, value_pos=Gtk.PositionType.LEFT, digits=1)
        scale.set_size_request(220, -1)
        scale.set_format_value_func(lambda _s, v: f"{v:+.1f} dB")
        scale.connect("value-changed", self._debounced(apply))
        return scale

    def connect_device(self):
        if self.mic:
            self.mic.close()
            self.mic = None
        try:
            self.mic = solocast2.SoloCast2()
            self.mixer = solocast2.Mixer()
            self.load()
        except solocast2.DeviceError as e:
            self.error.set_description(str(e))
            self._show(self.error)
            return
        self._show(self.page)

    def _show(self, widget):
        # Re-setting the current child trips an Adwaita assertion, so only swap when it changes.
        if self.toasts.get_child() is not widget:
            self.toasts.set_child(widget)

    def load(self):
        self._loading = True
        try:
            gain = self.mixer.get_gain()  # also caches the range, so gain_range() is free
            self.gain.set_range(*self.mixer.gain_range())
            self.gain.set_value(gain)
            self.mute.set_active(self.mixer.get_muted())
            hp, lp = self.mic.get_filters()
            self.high_pass.set_active(hp)
            self.low_pass.set_active(lp)
            self.boost.set_value(self.mic.get_presence_boost())
        finally:
            self._loading = False

    def _run(self, fn, *args):
        try:
            fn(*args)
        except solocast2.DeviceError as e:
            self.toasts.add_toast(Adw.Toast(title=f"Failed: {e}"))

    def _debounced(self, apply):
        pending = None

        def handler(scale):
            nonlocal pending
            if self._loading:
                return
            if pending:
                GLib.source_remove(pending)

            def fire():
                nonlocal pending
                pending = None
                self._run(apply, scale.get_value())
                return GLib.SOURCE_REMOVE

            pending = GLib.timeout_add(DEBOUNCE_MS, fire)

        return handler

    def _apply_gain(self, db):
        self.mixer.set_gain(db)

    def _apply_boost(self, db):
        self.mic.set_presence_boost(db)

    def _on_mute(self, row, _pspec):
        if not self._loading:
            self._run(self.mixer.set_muted, row.get_active())

    def _on_filter(self, *_):
        if not self._loading:
            self._run(self.mic.set_filters, self.high_pass.get_active(), self.low_pass.get_active())


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        (self.get_active_window() or Window(self)).present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
