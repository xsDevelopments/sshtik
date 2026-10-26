"""Main window: toolbar + tabs of SSH terminals. F5 = files, F6 = database.
Ctrl+Shift+T new connection, Ctrl+Shift+W close tab, Ctrl+PgUp/PgDn switch tabs."""
import argparse
import os

import gi
import paramiko
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk

from .config import config
from .connection import (SSHConnection, agent_auth_hint, close_all, open_jumps,
                         resolve_ssh_config)
from .terminal import SSHTerminal
from .filepanel import FilePanel
from .dbpanel import DBPanel
from .dbcommander import DBCommander
from .helpers import HistoryHelper, ManHelper
from .ui import install_css
from .about import show_about, build_summary, app_version, toolbar_icon


def ssh_config_hosts():
    """Named Host entries from ~/.ssh/config (skips wildcards)."""
    path = os.path.expanduser("~/.ssh/config")
    if not os.path.exists(path):
        return []
    cfg = paramiko.SSHConfig()
    with open(path) as f:
        cfg.parse(f)
    out = []
    for h in sorted(cfg.get_hostnames()):
        if any(c in h for c in "*?!"):
            continue
        d = cfg.lookup(h)
        out.append({"name": h, "host": h, "user": d.get("user", ""), "port": int(d.get("port", 22)),
                    "source": "ssh_config"})
    return out


class ConnectDialog(Gtk.Dialog):
    """Saved hosts list on the left, connection form on the right."""
    def __init__(self, parent):
        super().__init__(title="Connect", transient_for=parent, flags=0)
        self.add_buttons("Connect", Gtk.ResponseType.OK, "Cancel", Gtk.ResponseType.CANCEL)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(620, 360)
        self.result = None

        self.filter_text = ""
        self.store = Gtk.ListStore(str, str, str, int, str)  # name, host, user, port, source
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_enable_search(False)   # we provide our own substring filter below
        self.view.append_column(Gtk.TreeViewColumn("Saved hosts", Gtk.CellRendererText(), text=0))
        self.view.get_selection().connect("changed", self._selected)
        self.view.connect("row-activated", lambda *a: self.response(Gtk.ResponseType.OK))
        self.search = Gtk.Entry()
        self.search.set_placeholder_text("Filter hosts…  (↑/↓ to pick, Enter to connect)")
        self.search.connect("changed", lambda e: (setattr(self, "filter_text", e.get_text()), self._reload()))
        self.search.connect("key-press-event", self._on_search_key)
        sw = Gtk.ScrolledWindow(); sw.add(self.view)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left.set_size_request(240, -1)
        left.pack_start(self.search, False, False, 0)
        left.pack_start(sw, True, True, 0)

        grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin=12)
        self.entries = {}
        for i, (k, label) in enumerate((("name", "Name (optional)"), ("host", "Host"),
                                        ("user", "User"), ("port", "Port"))):
            grid.attach(Gtk.Label(label=label, xalign=1), 0, i, 1, 1)
            e = Gtk.Entry(activates_default=True)
            if k == "port": e.set_placeholder_text("22")
            self.entries[k] = e; grid.attach(e, 1, i, 1, 1)
        btns = Gtk.Box(spacing=6)
        save = Gtk.Button(label="Save"); save.connect("clicked", self._save)
        rm = Gtk.Button(label="Remove"); rm.connect("clicked", self._remove)
        btns.pack_start(save, False, False, 0); btns.pack_start(rm, False, False, 0)
        grid.attach(btns, 1, 4, 1, 1)
        grid.attach(Gtk.Label(label="Entries from ~/.ssh/config appear below your saved hosts.",
                              xalign=0, wrap=True), 0, 5, 2, 1)

        self._reload()                  # after self.entries exists (auto-select fills the form)
        box = Gtk.Box(spacing=8)
        box.pack_start(left, False, False, 0)
        box.pack_start(grid, True, True, 0)
        self.get_content_area().add(box)
        self.show_all()
        self.search.grab_focus()        # start typing to filter straight away

    def _reload(self):
        self.store.clear()
        q = self.filter_text.strip().lower()
        def ok(*fields):                # match the host name, hostname or user
            return not q or any(q in (f or "").lower() for f in fields)
        for h in sorted(config["hosts"], key=lambda h: (h.get("name") or h["host"]).lower()):
            name = h.get("name") or h["host"]
            if ok(name, h["host"], h.get("user", "")):
                self.store.append([name, h["host"], h.get("user", ""), int(h.get("port") or 22), "saved"])
        for h in ssh_config_hosts():
            if ok(h["name"], h["host"], h["user"]):
                self.store.append([h["name"] + "  (ssh config)", h["host"], h["user"], h["port"], "ssh_config"])
        if len(self.store):             # keep a match selected so Enter connects to it
            self.view.get_selection().select_path(Gtk.TreePath(0))

    def _on_search_key(self, _w, ev):
        if ev.keyval in (Gdk.KEY_Down, Gdk.KEY_Up) and len(self.store):
            sel = self.view.get_selection()
            _, rows = sel.get_selected_rows()
            cur = rows[0].get_indices()[0] if rows else -1
            p = Gtk.TreePath(max(0, min(len(self.store) - 1, cur + (1 if ev.keyval == Gdk.KEY_Down else -1))))
            sel.unselect_all(); sel.select_path(p); self.view.scroll_to_cell(p, None, False, 0, 0)
            return True
        if ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.response(Gtk.ResponseType.OK); return True
        return False

    def _selected(self, sel):
        model, it = sel.get_selected()
        if not it:
            return
        name, host, user, port, source = model[it]
        self.entries["name"].set_text(name if source == "saved" else "")
        self.entries["host"].set_text(host)
        self.entries["user"].set_text(user)
        self.entries["port"].set_text("" if port == 22 else str(port))

    def values(self):
        v = {k: e.get_text().strip() for k, e in self.entries.items()}
        v["port"] = int(v["port"]) if v["port"] else None
        return v

    def _save(self, *_):
        v = self.values()
        if not v["host"]:
            return
        hosts = [h for h in config["hosts"] if h["host"] != v["host"] or h.get("user") != v["user"]]
        hosts.append({"name": v["name"], "host": v["host"], "user": v["user"], "port": v["port"] or 22})
        config["hosts"] = hosts; config.save(); self._reload()

    def _remove(self, *_):
        model, it = self.view.get_selection().get_selected()
        if not it or model[it][4] != "saved":
            return
        host, user = model[it][1], model[it][2]
        config["hosts"] = [h for h in config["hosts"] if not (h["host"] == host and h.get("user", "") == user)]
        config.save(); self._reload()


class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="sshTIK")
        w, h = config["window"].get("main", (1100, 700))
        self.set_default_size(w, h)
        self.connect("delete-event", self._on_close)
        self.connect("destroy", Gtk.main_quit)

        tb = Gtk.Toolbar()
        def add_buttons(items):
            for label, icon, cb in items:
                b = Gtk.ToolButton(icon_name=icon, label=label)
                b.set_is_important(True)
                b.connect("clicked", cb)
                tb.insert(b, -1)
        # session actions on the left; the reference/history helpers on the right
        add_buttons((("Connect (F4)", "network-server", self.on_connect),
                     ("Files (F5)", "folder", self.on_files),
                     ("Database (F6)", "x-office-spreadsheet", self.on_db)))
        spacer = Gtk.SeparatorToolItem()
        spacer.set_draw(False); spacer.set_expand(True)
        tb.insert(spacer, -1)
        add_buttons((("Reference (F1)", "help-contents", self.on_manhelp),
                     ("History (F2)", "document-open-recent", self.on_history)))
        tb.insert(self._about_item(), -1)

        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(tb, False, False, 0)
        box.pack_start(self.notebook, True, True, 0)
        self.add(box)
        self.connect("key-press-event", self.on_key)

    def _about_item(self):
        """Quiet 'v0.1.2 [fish]' affordance at the toolbar's right edge."""
        item = Gtk.ToolItem()
        btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
        btn.set_tooltip_text("About sshTIK")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl = Gtk.Label(label="v" + app_version())
        lbl.get_style_context().add_class("dim-label")
        row.pack_start(lbl, False, False, 0)
        icon = toolbar_icon()
        if icon is not None:
            row.pack_start(Gtk.Image.new_from_pixbuf(icon), False, False, 0)
        btn.add(row)
        btn.connect("clicked", lambda b: show_about(self))
        item.add(btn)
        return item

    def _on_close(self, *_):
        config["window"]["main"] = list(self.get_size())
        config.save()
        return False

    def current(self):
        page = self.notebook.get_nth_page(self.notebook.get_current_page())
        return page.get_child() if page else None  # ScrolledWindow -> terminal

    def on_key(self, w, ev):
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        shift = ev.state & Gdk.ModifierType.SHIFT_MASK
        # Plain F-keys only: Shift/Ctrl/Alt+F-key goes to the terminal, so
        # programs in it (tmux, sshtac, mc) can use them.
        plain = not (ctrl or shift or ev.state & Gdk.ModifierType.MOD1_MASK)
        if plain and ev.keyval == Gdk.KEY_F1: self.on_manhelp(); return True
        if plain and ev.keyval == Gdk.KEY_F2: self.on_history(); return True
        if plain and ev.keyval == Gdk.KEY_F4: self.on_connect(); return True
        if plain and ev.keyval == Gdk.KEY_F5: self.on_files(); return True
        if plain and ev.keyval == Gdk.KEY_F6: self.on_db(); return True
        if ctrl and shift and ev.keyval in (Gdk.KEY_T, Gdk.KEY_t): self.on_connect(); return True
        if ctrl and shift and ev.keyval in (Gdk.KEY_W, Gdk.KEY_w): self.close_tab(); return True
        if ctrl and ev.keyval == Gdk.KEY_Page_Down: self.notebook.next_page(); return True
        if ctrl and ev.keyval == Gdk.KEY_Page_Up: self.notebook.prev_page(); return True
        return False

    def on_connect(self, *_):
        d = ConnectDialog(self)
        if d.run() == Gtk.ResponseType.OK:
            v = d.values(); d.destroy()
            if v["host"]:
                self.open_tab(v["host"], v["user"] or None, v["port"])
        else:
            d.destroy()

    def _ask_password(self, prompt, hint=None):
        d = Gtk.Dialog(title="Password", transient_for=self, flags=0)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        box = d.get_content_area(); box.set_spacing(6); box.set_margin_start(12); box.set_margin_end(12)
        if hint:
            h = Gtk.Label(label=hint, xalign=0); h.set_line_wrap(True); h.set_max_width_chars(52)
            h.get_style_context().add_class("dim-label"); box.add(h)
        box.add(Gtk.Label(label=prompt))
        e = Gtk.Entry(visibility=False, activates_default=True); box.add(e)
        d.set_default_response(Gtk.ResponseType.OK); d.show_all()
        pw = e.get_text() if d.run() == Gtk.ResponseType.OK else None
        d.destroy()
        return pw

    def _connect(self, host, user, port, via=None, resolved=None):
        """Open an SSHConnection, prompting for a password on auth failure.
        Returns None if the user cancels or the connection fails."""
        jumps = []
        if via is None and resolved is None:
            # ProxyJump: open each hop here so every hop gets its own password
            # prompt and a retry on the target doesn't reconnect the chain
            hops = resolve_ssh_config(host, user, port)["proxyjump"]
            if hops:
                jumps = open_jumps(hops, lambda h, u, p, v: self._connect(h, u, p, via=v))
                if jumps is None:
                    return None
                via = jumps[-1]
        password = None
        where = f"{(resolved or {}).get('user') or user or ''}@{host}" + (f" (via {via.host})" if via else "")
        while True:
            try:
                return SSHConnection(host, user=user, port=port, password=password,
                                     via=via, resolved=resolved, jumps=jumps)
            except paramiko.AuthenticationException:
                password = self._ask_password(f"Password for {where}:",
                                              hint=agent_auth_hint())
                if password is None:
                    close_all(jumps)
                    return None
            except Exception as e:
                close_all(jumps)
                md = Gtk.MessageDialog(transient_for=self, message_type=Gtk.MessageType.ERROR,
                                       buttons=Gtk.ButtonsType.OK, text=f"{where}: {e}")
                md.run(); md.destroy(); return None

    def open_tab(self, host, user, port):
        conn = self._connect(host, user, port)
        if conn is None:
            return
        term = SSHTerminal(conn)
        term.hops = [conn]  # connection chain; grows when the user ssh's onward
        sw = Gtk.ScrolledWindow(); sw.add(term)
        sw.get_style_context().add_class("terminal-frame")

        label = Gtk.Box(spacing=4)
        label.pack_start(Gtk.Label(label=f"{conn.user + '@' if conn.user else ''}{host}"), True, True, 0)
        x = Gtk.Button.new_from_icon_name("window-close", Gtk.IconSize.MENU)
        x.set_relief(Gtk.ReliefStyle.NONE)
        x.connect("clicked", lambda b: self.close_tab(sw))
        label.pack_start(x, False, False, 0)
        label.show_all()

        self.notebook.append_page(sw, label)
        self.notebook.set_tab_reorderable(sw, True)
        self.notebook.show_all()
        self.notebook.set_current_page(-1)
        term.grab_focus()

    def active_connection(self, term):
        """Follow nested `ssh` sessions: if the shell on hop N is running an
        ssh client, tunnel a connection to its target through hop N and use
        that. Live hops are cached on the terminal and reused."""
        chain = term.hops
        i = 0
        while True:
            conn = chain[i]
            tgt = conn.hop_target()
            if not tgt:
                for c in chain[i + 1:]:
                    c.close()
                del chain[i + 1:]
                return conn
            key = (tgt["hostname"], tgt["user"], tgt["port"])
            if len(chain) > i + 1 and getattr(chain[i + 1], "key", None) == key and chain[i + 1].alive():
                i += 1
                continue
            for c in chain[i + 1:]:
                c.close()
            del chain[i + 1:]
            new = self._connect(tgt["label"], tgt["user"], tgt["port"], via=conn, resolved=tgt)
            if new is None:
                return None
            new.key = key
            new.find_session_shell(conn.my_addresses(tgt["ssh_pid"]))
            chain.append(new)
            i += 1

    def close_tab(self, page=None):
        if page is None:
            page = self.notebook.get_nth_page(self.notebook.get_current_page())
        if page is None:
            return
        term = page.get_child()
        for c in reversed(getattr(term, "hops", [term.conn])):
            try: c.close()
            except Exception: pass
        self.notebook.remove_page(self.notebook.page_num(page))

    def on_files(self, *_):
        t = self.current()
        if not t: return
        conn = self.active_connection(t)
        if conn: FilePanel(conn, parent=self).show_all()

    def on_db(self, *_):
        t = self.current()
        if not t: return
        conn = self.active_connection(t)
        if conn: DBCommander(conn, parent=self).show_all()

    def on_manhelp(self, *_):
        ManHelper(self, self.current()).show_all()   # works without a connection

    def on_history(self, *_):
        t = self.current()
        if not t: return
        conn = self.active_connection(t)
        if conn: HistoryHelper(self, conn, t).show_all()


def main():
    ap = argparse.ArgumentParser(description="SSH shell with SFTP/DB helpers")
    ap.add_argument("target", nargs="?", help="[user@]host (honours ~/.ssh/config)")
    ap.add_argument("-p", "--port", type=int, default=None)
    ap.add_argument("-V", "--version", action="store_true", help="print version and exit")
    args = ap.parse_args()
    if args.version:
        print(build_summary()); return

    install_css()
    w = MainWindow()
    w.show_all()
    if args.target:
        user, _, host = args.target.rpartition("@")
        w.open_tab(host, user or None, args.port)
    else:
        w.on_connect()
    Gtk.main()
