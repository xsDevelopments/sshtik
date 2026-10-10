"""Small shared UI helpers: app-wide CSS and zebra striping for tree views."""
import threading
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

_CSS = b"""
/* 8px breathing room around the terminal: margin on the VTE widget, with the
   scrolled window behind it painted the terminal's background colour. */
.terminal-frame { background-color: #000000; }

/* Dual-pane focus: exactly one blue highlight, always where the keyboard focus
   is. The pane whose list has focus shows its selected row in the accent
   colour; every other selection is a muted grey. A location bar is blue only
   while it is focused (its text selection is cleared on focus-out), so a pane
   you tab away from never keeps a stray blue bar. No frame -- the single blue
   highlight is the whole focus cue. */
.sshtik-pane treeview:selected {
    background-color: mix(@theme_bg_color, @theme_fg_color, 0.22);
    color: @theme_fg_color;
}
.sshtik-pane.list-focused treeview:selected {
    background-color: @theme_selected_bg_color;
    color: @theme_selected_fg_color;
}
"""
TERMINAL_MARGIN = 8

_STRIPE = Gdk.RGBA(0.5, 0.5, 0.5, 0.14)  # translucent grey: lighter on dark themes, darker on light


def install_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _stripe_cell(column, renderer, model, it, _data):
    odd = model.get_path(it).get_indices()[0] % 2 == 1
    renderer.set_property("cell-background-rgba", _STRIPE if odd else None)
    renderer.set_property("cell-background-set", odd)


CELL_XPAD, CELL_YPAD = 6, 3  # breathing room inside grid cells (like html cellpadding)


def pad(renderer):
    renderer.set_property("xpad", CELL_XPAD)
    renderer.set_property("ypad", CELL_YPAD)
    return renderer


def stripe(treeview):
    """Shade every second row. Call after all columns are added."""
    for col in treeview.get_columns():
        for rend in col.get_cells():
            col.set_cell_data_func(rend, _stripe_cell, None)


def close_on_escape(window):
    """Close a helper window on Esc — unless a child (e.g. a cell being
    edited) consumed the key first, which is why this is connect_after."""
    def on_key(w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            w.close(); return True
        return False
    window.connect_after("key-press-event", on_key)


def _human_size(n):
    n = float(n or 0)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{int(n)} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def _fmt_eta(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


class TransferProgress(Gtk.Window):
    """Modal progress window for a long copy/transfer: a bar with a percentage,
    a detail line (done / total · speed · ETA) and a Cancel button. A worker
    thread polls `cancelled` (a threading.Event) and drives the display through
    `set_heading` / `update` / `finish` via GLib.idle_add.

    Cancelling is two-step by keyboard, so it can't happen by a stray Enter:
    Esc *arms* Cancel (focuses it, makes it the default, turns it red); Enter
    then confirms. Clicking Cancel, or the window's close button, cancels too."""
    def __init__(self, parent, title):
        super().__init__(title=title)
        if parent is not None:
            self.set_transient_for(parent)
        self.set_modal(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_default_size(440, -1)
        self.set_resizable(False)
        self.cancelled = threading.Event()
        self._start = time.monotonic()

        self.heading = Gtk.Label(xalign=0)
        self.heading.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.bar = Gtk.ProgressBar(show_text=True)
        self.detail = Gtk.Label(xalign=0)
        self.detail.get_style_context().add_class("dim-label")
        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.set_can_default(True)
        self.cancel_btn.connect("clicked", lambda _b: self._do_cancel())
        btn_row = Gtk.Box(); btn_row.set_halign(Gtk.Align.END)
        btn_row.pack_start(self.cancel_btn, False, False, 0)

        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("start", "end", "top", "bottom"):
            getattr(vb, f"set_margin_{m}")(14)
        vb.pack_start(self.heading, False, False, 0)
        vb.pack_start(self.bar, False, False, 0)
        vb.pack_start(self.detail, False, False, 0)
        vb.pack_start(btn_row, False, False, 0)
        self.add(vb)
        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", lambda w, e: (self._do_cancel(), True)[1])

    def _on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:          # arm Cancel; Enter then confirms
            if not self.cancelled.is_set():
                self.cancel_btn.grab_focus()
                self.cancel_btn.grab_default()
                self.cancel_btn.get_style_context().add_class("destructive-action")
                self.cancel_btn.set_label("Cancel  (press Enter)")
            return True
        return False

    def _do_cancel(self):
        if not self.cancelled.is_set():
            self.cancelled.set()
            self.cancel_btn.set_sensitive(False)
            self.cancel_btn.set_label("Cancelling…")

    # ---- called via GLib.idle_add from the worker thread ----------------
    def reset_timer(self):
        self._start = time.monotonic()
        return False

    def set_heading(self, text):
        self.heading.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>")
        return False

    def update(self, done, total, extra=""):
        frac = (done / total) if total else 0.0
        self.bar.set_fraction(min(max(frac, 0.0), 1.0))
        self.bar.set_text(f"{min(frac, 1.0) * 100:.0f}%")
        elapsed = time.monotonic() - self._start
        speed = done / elapsed if elapsed > 0 else 0
        parts = [f"{_human_size(done)} of {_human_size(total)}" if total else _human_size(done)]
        if speed > 0 and elapsed > 0.8:          # let the rate settle before showing it
            parts.append(_human_size(int(speed)) + "/s")
            if total and 0 < done <= total:
                parts.append("~" + _fmt_eta((total - done) / speed) + " left")
        if extra:
            parts.append(extra)
        self.detail.set_text("   ·   ".join(parts))
        return False

    def finish(self):
        self.destroy()
        return False
