"""Small shared UI helpers: app-wide CSS and zebra striping for tree views."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk

_CSS = b"""
/* 8px breathing room around the terminal: margin on the VTE widget, with the
   scrolled window behind it painted the terminal's background colour. */
.terminal-frame { background-color: #000000; }
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


def stripe(treeview):
    """Shade every second row. Call after all columns are added."""
    for col in treeview.get_columns():
        for rend in col.get_cells():
            col.set_cell_data_func(rend, _stripe_cell, None)
