"""VTE terminal widget wired to a paramiko shell channel."""
import os
import re
import threading
import tty

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Gdk, Vte, GLib

from .config import config
from .ui import TERMINAL_MARGIN

# Marker we ask the remote shell to print once so we learn its PID.
_PID_MARKER = "__SSHPANEL_PID__"
_PID_RE = re.compile(rb"__SSHPANEL_PID__=(\d+)")


class SSHTerminal(Vte.Terminal):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.set_scrollback_lines(10000)
        self.set_font_scale(config.get("font_scale", 1.0))
        self.set_mouse_autohide(True)
        for side in ("start", "end", "top", "bottom"):
            getattr(self, f"set_margin_{side}")(TERMINAL_MARGIN)
        self.connect("key-press-event", self._on_key)
        self.connect("button-press-event", self._on_button)

        # Bidirectional pty so VTE thinks it has a real child.
        master, slave = os.openpty()
        tty.setraw(slave)  # pass every byte through immediately, no local echo/ISIG
        self._pty = Vte.Pty.new_foreign_sync(master)
        self.set_pty(self._pty)
        self._slave = slave

        self.chan = conn.open_shell(cols=self.get_column_count(),
                                    rows=self.get_row_count())
        self.connect("size-allocate", self._on_resize)

        # Ask the shell for its PID, then bump prompt. Output is harmless
        # noise in scrollback; hide it with a clear if desired.
        self.chan.send(f' echo {_PID_MARKER}=$$; clear\n')

        threading.Thread(target=self._pump_remote_to_pty, daemon=True).start()
        threading.Thread(target=self._pump_pty_to_remote, daemon=True).start()

    # remote -> screen
    def _pump_remote_to_pty(self):
        while True:
            data = self.chan.recv(65536)
            if not data:
                GLib.idle_add(self.feed, b"\r\n[connection closed]\r\n")
                return
            if self.conn.shell_pid is None:
                m = _PID_RE.search(data)
                if m:
                    self.conn.shell_pid = int(m.group(1))
            os.write(self._slave, data)

    # keyboard -> remote
    def _pump_pty_to_remote(self):
        while True:
            try:
                data = os.read(self._slave, 4096)
            except OSError:
                return
            if data:
                self.chan.send(data)

    # ---- copy / paste / zoom ---------------------------------------------
    def _on_key(self, w, ev):
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        shift = ev.state & Gdk.ModifierType.SHIFT_MASK
        if ctrl and shift and ev.keyval in (Gdk.KEY_C, Gdk.KEY_c):
            self.copy_clipboard_format(Vte.Format.TEXT); return True
        if ctrl and shift and ev.keyval in (Gdk.KEY_V, Gdk.KEY_v):
            self.paste_clipboard(); return True
        if ctrl and ev.keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self._zoom(1.1); return True
        if ctrl and ev.keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self._zoom(1 / 1.1); return True
        if ctrl and ev.keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self._zoom(None); return True
        return False

    def _zoom(self, factor):
        scale = 1.0 if factor is None else max(0.4, min(4.0, self.get_font_scale() * factor))
        self.set_font_scale(scale)
        config["font_scale"] = scale
        config.save()

    def _on_button(self, w, ev):
        if ev.button != 3:
            return False
        menu = Gtk.Menu()
        for label, cb in (("Copy", lambda m: self.copy_clipboard_format(Vte.Format.TEXT)),
                          ("Paste", lambda m: self.paste_clipboard()),
                          ("Select all", lambda m: self.select_all())):
            mi = Gtk.MenuItem(label=label); mi.connect("activate", cb); menu.append(mi)
        menu.show_all(); menu.popup_at_pointer(ev)
        return True

    def _on_resize(self, *_):
        cols, rows = self.get_column_count(), self.get_row_count()
        if cols and rows and self.chan.active:
            try:
                self.chan.resize_pty(width=cols, height=rows)
            except Exception:
                pass
