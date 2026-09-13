"""Main window: toolbar + tabs of SSH terminals. F5 = files, F6 = database.
Ctrl+Shift+T new connection, Ctrl+Shift+W close tab, Ctrl+PgUp/PgDn switch tabs."""
import argparse
import os

import gi
import paramiko
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk

from .config import config
from .connection import SSHConnection
from .terminal import SSHTerminal
from .filepanel import FilePanel
from .dbpanel import DBPanel
from .ui import install_css
from .about import show_about, build_summary


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
        self.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Connect", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(620, 360)
        self.result = None

        self.store = Gtk.ListStore(str, str, str, int, str)  # name, host, user, port, source
        self._reload()
        self.view = Gtk.TreeView(model=self.store)
        self.view.append_column(Gtk.TreeViewColumn("Saved hosts", Gtk.CellRendererText(), text=0))
        self.view.get_selection().connect("changed", self._selected)
        self.view.connect("row-activated", lambda *a: self.response(Gtk.ResponseType.OK))
        sw = Gtk.ScrolledWindow(); sw.add(self.view); sw.set_size_request(240, -1)

        grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin=12)
        self.entries = {}
        for i, (k, label) in enumerate((("name", "Name (optional)"), ("host", "Host"),
                                        ("user", "User"), ("port", "Port"))):
            grid.attach(Gtk.Label(label=label, xalign=1), 0, i, 1, 1)
            e = Gtk.Entry(activates_default=True)
            if k == "port": e.set_placeholder_text("22")
            self.entries[k] = e; grid.attach(e, 1, i, 1, 1)
        self.entries["host"].grab_focus()
        btns = Gtk.Box(spacing=6)
        save = Gtk.Button(label="Save"); save.connect("clicked", self._save)
        rm = Gtk.Button(label="Remove"); rm.connect("clicked", self._remove)
        btns.pack_start(save, False, False, 0); btns.pack_start(rm, False, False, 0)
        grid.attach(btns, 1, 4, 1, 1)
        grid.attach(Gtk.Label(label="Entries from ~/.ssh/config appear below your saved hosts.",
                              xalign=0, wrap=True), 0, 5, 2, 1)

        box = Gtk.Box(spacing=8)
        box.pack_start(sw, False, False, 0)
        box.pack_start(grid, True, True, 0)
        self.get_content_area().add(box)
        self.show_all()

    def _reload(self):
        self.store.clear()
        for h in config["hosts"]:
            self.store.append([h.get("name") or h["host"], h["host"], h.get("user", ""), int(h.get("port") or 22), "saved"])
        for h in ssh_config_hosts():
            self.store.append([h["name"] + "  (ssh config)", h["host"], h["user"], h["port"], "ssh_config"])

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
        for label, icon, cb in (("Connect (F4)", "network-server", self.on_connect),
                                ("Files (F5)", "folder", self.on_files),
                                ("Database (F6)", "x-office-spreadsheet", self.on_db)):
            b = Gtk.ToolButton(icon_name=icon, label=label)
            b.set_is_important(True)
            b.connect("clicked", cb)
            tb.insert(b, -1)
        spacer = Gtk.SeparatorToolItem()
        spacer.set_draw(False); spacer.set_expand(True)
        tb.insert(spacer, -1)
        about = Gtk.ToolButton(icon_name="help-about", label="About")
        about.connect("clicked", lambda b: show_about(self))
        tb.insert(about, -1)

        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(tb, False, False, 0)
        box.pack_start(self.notebook, True, True, 0)
        self.add(box)
        self.connect("key-press-event", self.on_key)

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
        if ev.keyval == Gdk.KEY_F4: self.on_connect(); return True
        if ev.keyval == Gdk.KEY_F5: self.on_files(); return True
        if ev.keyval == Gdk.KEY_F6: self.on_db(); return True
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

    def _ask_password(self, prompt):
        d = Gtk.Dialog(title="Password", transient_for=self, flags=0)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        box = d.get_content_area(); box.set_spacing(6); box.set_margin_start(12); box.set_margin_end(12)
        box.add(Gtk.Label(label=prompt))
        e = Gtk.Entry(visibility=False, activates_default=True); box.add(e)
        d.set_default_response(Gtk.ResponseType.OK); d.show_all()
        pw = e.get_text() if d.run() == Gtk.ResponseType.OK else None
        d.destroy()
        return pw

    def _connect(self, host, user, port, via=None, resolved=None):
        """Open an SSHConnection, prompting for a password on auth failure.
        Returns None if the user cancels or the connection fails."""
        password = None
        where = f"{(resolved or {}).get('user') or user or ''}@{host}" + (f" (via {via.host})" if via else "")
        while True:
            try:
                return SSHConnection(host, user=user, port=port, password=password,
                                     via=via, resolved=resolved)
            except paramiko.AuthenticationException:
                password = self._ask_password(f"Password for {where}:")
                if password is None:
                    return None
            except Exception as e:
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
        if conn: DBPanel(conn, parent=self).show_all()


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
