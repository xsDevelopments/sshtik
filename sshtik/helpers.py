"""Pop-up helpers driven from the terminal:

  F1  ManHelper      — a searchable quick-reference of common commands
  F2  HistoryHelper  — live search of the remote shell's bash history

Both can drop a chosen command onto the current prompt (typed, not run — you
press Enter yourself)."""
import re
import threading

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

from .ui import close_on_escape
from .quickref import COMMANDS

_TS = re.compile(r"^#\d+$")   # bash HISTTIMEFORMAT timestamp marker lines


def _move_selection(view, delta):
    """Move a tree view's selection by delta without taking keyboard focus
    (so arrow keys work while the user is still typing in a search box)."""
    model = view.get_model()
    if len(model) == 0:
        return
    sel = view.get_selection()
    _, rows = sel.get_selected_rows()
    cur = rows[0].get_indices()[0] if rows else -1
    nxt = max(0, min(len(model) - 1, cur + delta))
    path = Gtk.TreePath(nxt)
    sel.unselect_all(); sel.select_path(path)
    view.scroll_to_cell(path, None, False, 0, 0)


def _selected_index(view):
    _, rows = view.get_selection().get_selected_rows()
    return rows[0].get_indices()[0] if rows else -1


# ---------------------------------------------------------------------------
class HistoryHelper(Gtk.Window):
    """Search the remote shell's ~/.bash_history (newest first). Type to filter;
    Up/Down to pick; Enter drops it on the prompt."""
    def __init__(self, parent, conn, term):
        super().__init__(title=f"History — {conn.host}")
        self.set_transient_for(parent); self.set_default_size(720, 460)
        close_on_escape(self)
        self.term = term
        self.all = []

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Type to search history…  (Up/Down to pick, Enter to use)")
        self.entry.connect("changed", lambda _e: self._refilter())
        self.entry.connect("key-press-event", self._on_entry_key)

        self.store = Gtk.ListStore(str)
        self.view = Gtk.TreeView(model=self.store, headers_visible=False)
        self.view.append_column(Gtk.TreeViewColumn("cmd", Gtk.CellRendererText(), text=0))
        self.view.connect("row-activated", lambda *a: self._use())
        sw = Gtk.ScrolledWindow(); sw.add(self.view)

        self.status = Gtk.Label(label="Loading history…", xalign=0)
        self.status.get_style_context().add_class("dim-label")
        self.status.set_margin_start(8)

        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("start", "end", "top", "bottom"):
            getattr(vb, f"set_margin_{m}")(8)
        vb.pack_start(self.entry, False, False, 0)
        vb.pack_start(sw, True, True, 0)
        vb.pack_start(self.status, False, False, 0)
        self.add(vb)
        self.entry.grab_focus()
        threading.Thread(target=self._load, args=(conn,), daemon=True).start()

    def _load(self, conn):
        try:
            _, out, _ = conn.run("tail -n 5000 ~/.bash_history 2>/dev/null")
        except Exception as e:
            GLib.idle_add(self.status.set_text, f"Couldn't read history: {e}"); return
        lines, seen = [], set()
        for ln in reversed(out.splitlines()):      # newest first
            ln = ln.rstrip()
            if not ln or _TS.match(ln) or ln in seen:
                continue
            seen.add(ln); lines.append(ln)
        GLib.idle_add(self._loaded, lines)

    def _loaded(self, lines):
        self.all = lines
        self._refilter()
        return False

    def _refilter(self):
        q = self.entry.get_text().strip().lower()
        self.store.clear()
        n = 0
        for cmd in self.all:
            if not q or q in cmd.lower():
                self.store.append([cmd]); n += 1
                if n >= 2000:                      # keep the view snappy
                    break
        if len(self.store):
            self.view.get_selection().select_path(Gtk.TreePath(0))
        total = len(self.all)
        self.status.set_text(f"{n} match(es)" + (f" of {total} commands" if q else f" ({total} commands)")
                             if total else "No history found (~/.bash_history empty or unreadable)")

    def _on_entry_key(self, _w, ev):
        if ev.keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            _move_selection(self.view, 1 if ev.keyval == Gdk.KEY_Down else -1); return True
        if ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._use(); return True
        return False

    def _use(self):
        idx = _selected_index(self.view)
        if idx < 0 and len(self.store):
            idx = 0
        if idx < 0:
            return
        cmd = self.store[idx][0]
        if self.term is not None:
            self.term.feed_command(cmd)
            GLib.idle_add(self.term.grab_focus)
        self.close()


# ---------------------------------------------------------------------------
class ManHelper(Gtk.Window):
    """A curated quick-reference. Type to filter commands (by name, summary or
    example text); pick one to see its examples; Enter/double-click an example
    drops it on the prompt."""
    def __init__(self, parent, term):
        super().__init__(title="Command reference")
        self.set_transient_for(parent); self.set_default_size(820, 500)
        close_on_escape(self)
        self.term = term

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Filter commands…  (name, flag or keyword — e.g. 'ls', 'port', 'tar')")
        self.entry.connect("changed", lambda _e: self._refilter())
        self.entry.connect("key-press-event", self._on_entry_key)

        self.cmd_store = Gtk.ListStore(str, str, int)    # name, summary, index into COMMANDS
        self.cmd_view = Gtk.TreeView(model=self.cmd_store, headers_visible=False)
        self.cmd_view.append_column(Gtk.TreeViewColumn("name",
            Gtk.CellRendererText(weight=700), text=0))
        self.cmd_view.append_column(Gtk.TreeViewColumn("summary",
            Gtk.CellRendererText(ellipsize=3), text=1))
        self.cmd_view.get_selection().connect("changed", lambda _s: self._show_cmd())
        lsw = Gtk.ScrolledWindow(); lsw.add(self.cmd_view); lsw.set_size_request(280, -1)

        self.header = Gtk.Label(xalign=0); self.header.set_margin_bottom(4)
        self.ex_store = Gtk.ListStore(str, str)          # description, command
        self.ex_view = Gtk.TreeView(model=self.ex_store, headers_visible=True)
        whatcol = Gtk.TreeViewColumn("What", Gtk.CellRendererText(ellipsize=3), text=0)
        whatcol.set_min_width(190); whatcol.set_resizable(True)
        self.ex_view.append_column(whatcol)
        cmdcol = Gtk.TreeViewColumn("Command", Gtk.CellRendererText(family="monospace"), text=1)
        cmdcol.set_expand(True); cmdcol.set_resizable(True)
        self.ex_view.append_column(cmdcol)
        self.ex_view.connect("row-activated", lambda *a: self._use())
        esw = Gtk.ScrolledWindow(); esw.add(self.ex_view)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        right.pack_start(self.header, False, False, 0)
        right.pack_start(esw, True, True, 0)

        paned = Gtk.Paned(); paned.pack1(lsw, False, False); paned.pack2(right, True, False)
        hint = Gtk.Label(xalign=0, label="Enter/double-click an example to drop it on the prompt (you press Enter to run).")
        hint.get_style_context().add_class("dim-label"); hint.set_margin_start(4)
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("start", "end", "top", "bottom"):
            getattr(vb, f"set_margin_{m}")(8)
        vb.pack_start(self.entry, False, False, 0)
        vb.pack_start(paned, True, True, 0)
        vb.pack_start(hint, False, False, 0)
        self.add(vb)
        self.entry.grab_focus()
        self._refilter()

    @staticmethod
    def _matches(cmd, q):
        if q in cmd["name"].lower() or q in cmd["summary"].lower() or q in cmd.get("notes", "").lower():
            return True
        return any(q in d.lower() or q in c.lower() for d, c in cmd["examples"])

    def _refilter(self):
        q = self.entry.get_text().strip().lower()
        self.cmd_store.clear()
        for i, cmd in enumerate(COMMANDS):
            if not q or self._matches(cmd, q):
                self.cmd_store.append([cmd["name"], cmd["summary"], i])
        if len(self.cmd_store):
            self.cmd_view.get_selection().select_path(Gtk.TreePath(0))
        else:
            self.ex_store.clear(); self.header.set_text("")

    def _show_cmd(self):
        idx = _selected_index(self.cmd_view)
        if idx < 0:
            return
        cmd = COMMANDS[self.cmd_store[idx][2]]
        note = f"   —   {cmd['notes']}" if cmd.get("notes") else ""
        self.header.set_markup(f"<b>{GLib.markup_escape_text(cmd['name'])}</b>"
                               f"   {GLib.markup_escape_text(cmd['summary'])}"
                               f"<small><span alpha='60%'>{GLib.markup_escape_text(note)}</span></small>")
        self.ex_store.clear()
        for desc, c in cmd["examples"]:
            self.ex_store.append([desc, c])

    def _on_entry_key(self, _w, ev):
        if ev.keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            _move_selection(self.cmd_view, 1 if ev.keyval == Gdk.KEY_Down else -1); return True
        if ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):   # jump to the examples
            if len(self.ex_store):
                self.ex_view.grab_focus()
                self.ex_view.set_cursor(Gtk.TreePath(0))
            return True
        return False

    def _use(self):
        idx = _selected_index(self.ex_view)
        if idx < 0:
            return
        cmd = self.ex_store[idx][1]
        if self.term is not None:
            self.term.feed_command(cmd)
            GLib.idle_add(self.term.grab_focus)
            self.close()
        else:                                      # no live terminal: copy instead
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(cmd, -1)
            self.close()
