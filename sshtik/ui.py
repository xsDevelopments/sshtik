"""Small shared UI helpers: app-wide CSS and zebra striping for tree views."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk

_CSS = b"""
/* 8px breathing room around the terminal: margin on the VTE widget, with the
   scrolled window behind it painted the terminal's background colour. */
.terminal-frame { background-color: #000000; }

/* Dual-pane focus: only the active pane reads as "live". Its selection keeps
   the accent colour and it gains an accent frame; the inactive pane's
   selection drops to the theme's muted (unfocused) selection colour, so blue
   points at exactly one side -- the one the next keystroke goes to. */
.sshtik-pane {
    border: 2px solid transparent;
    border-radius: 4px;
}
.sshtik-pane.active-pane {
    border-color: @theme_selected_bg_color;
    background-color: alpha(@theme_selected_bg_color, 0.13);
}
.sshtik-pane treeview:selected {
    background-color: mix(@theme_bg_color, @theme_fg_color, 0.22);
    color: @theme_fg_color;
}
.sshtik-pane.active-pane treeview:selected {
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
