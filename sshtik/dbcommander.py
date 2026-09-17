"""Norton-Commander-style MySQL browser: an SSH host's server on each side,
with copy/move of tables and databases between them. Not a HeidiSQL clone —
a transfer tool that speaks MySQL.

Each pane is a MySQLEndpoint that runs mysql/mysqldump on its SSH host over an
exec channel (socket auth on that box, so no per-host TCP grants are needed).
Either pane can be re-pointed at any host in your SSH config — including
localhost, to reach this machine's own server. F5 copies the highlighted table
or database to the other side (mysqldump | mysql); F6 moves it."""
import shlex
import threading

import paramiko

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango, GObject

from .config import config
from .connection import SSHConnection
from .ui import stripe, close_on_escape, pad
from .dbpanel import q_ident, q_val, _human_bytes, _parse_mysql_argv, AuthError

# Logins entered this session but not saved to disk (survives Esc + reopen).
_SESSION_LOGINS = {}


def _login_key(ep):
    return f"ssh:{ep.conn.host}"


def _apply_saved_login(ep):
    """Prefill an endpoint from a remembered (disk) or session-cached login."""
    key = _login_key(ep)
    data = config.get("db_logins", {}).get(key) or _SESSION_LOGINS.get(key)
    if not data:
        return False
    ep.opts.update(user=data.get("user"), host=data.get("host"), port=data.get("port"))
    ep.password = data.get("password")
    return True


def _store_login(ep, remember):
    """Cache the endpoint's login for the session; persist it if remember."""
    key = _login_key(ep)
    data = {"user": ep.opts["user"], "host": ep.opts["host"],
            "port": ep.opts["port"], "password": ep.password}
    _SESSION_LOGINS[key] = data
    logins = config.setdefault("db_logins", {})
    if remember:
        logins[key] = data; config.save()
    elif key in logins:
        del logins[key]; config.save()


# ---------------------------------------------------------------------------
class MySQLEndpoint:
    """mysql/mysqldump runner for one server, reached over an SSH connection."""
    def __init__(self, label, conn):
        self.label = label
        self.conn = conn
        self.opts = {"user": None, "host": None, "port": None, "db": None, "defaults": []}
        self.password = None
        self.available = None   # True, "auth" (needs password), or False
        self.last_error = None

    def target(self):
        o = self.opts
        return (o["user"] + "@" if o["user"] else "") + (o["host"] or self.conn.host)

    # ---- command execution ---------------------------------------------
    def _conn_args(self):
        o = self.opts
        a = list(o["defaults"])
        if o["user"]: a += ["-u", o["user"]]
        if o["host"]: a += ["-h", o["host"]]
        if o["port"]: a += ["-P", str(o["port"])]
        return a

    def _exec(self, argv, input=None, timeout=600):
        cmd = " ".join(shlex.quote(a) for a in argv)
        if self.password is not None:
            cmd = "MYSQL_PWD=" + shlex.quote(self.password) + " " + cmd
        return self.conn.run(cmd, timeout=timeout, input=input)

    def query(self, sql, db=None):
        argv = ["mysql", "--batch", "--raw"] + self._conn_args()
        use = db or self.opts["db"]
        if use:
            argv.append(use)
        argv += ["-e", sql]
        rc, out, err = self._exec(argv, timeout=120)
        if rc != 0:
            msg = err.strip() or f"mysql exited {rc}"
            if "Access denied" in msg and "using password" in msg:
                raise AuthError(msg)
            raise RuntimeError(msg)
        rows = [line.split("\t") for line in out.splitlines()]
        return (rows[0], rows[1:]) if rows else ([], [])

    def dump(self, db, table=None):
        argv = (["mysqldump", "--single-transaction", "--quick", "--no-tablespaces"]
                + self._conn_args() + [db])
        if table:
            argv.append(table)
        rc, out, err = self._exec(argv)
        if rc != 0:
            raise RuntimeError(err.strip() or f"mysqldump exited {rc}")
        return out

    def load(self, sql_text, db):
        argv = ["mysql"] + self._conn_args() + [db]
        rc, _, err = self._exec(argv, input=sql_text)
        if rc != 0:
            raise RuntimeError(err.strip() or f"mysql load exited {rc}")

    # ---- schema --------------------------------------------------------
    def probe(self):
        try:
            self.query("SELECT 1")
            self.available = True; self.last_error = None
        except AuthError as e:
            self.available = "auth"; self.last_error = str(e)
        except Exception as e:
            self.available = False; self.last_error = str(e)
        return self.available

    def databases(self):
        q = ("SELECT s.SCHEMA_NAME, COALESCE(SUM(t.DATA_LENGTH+t.INDEX_LENGTH),0) "
             "FROM information_schema.SCHEMATA s "
             "LEFT JOIN information_schema.TABLES t ON t.TABLE_SCHEMA=s.SCHEMA_NAME "
             "GROUP BY s.SCHEMA_NAME ORDER BY s.SCHEMA_NAME")
        _, rows = self.query(q)
        return rows  # [name, bytes]

    def tables(self, db):
        q = ("SELECT TABLE_NAME, COALESCE(TABLE_ROWS,0), "
             "COALESCE(DATA_LENGTH,0)+COALESCE(INDEX_LENGTH,0) "
             "FROM information_schema.TABLES "
             f"WHERE TABLE_SCHEMA={q_val(db)} AND TABLE_TYPE='BASE TABLE' "
             "ORDER BY TABLE_NAME")
        _, rows = self.query(q, db=db)
        return rows  # [name, rows, bytes]

    def create_database(self, name):
        self.query(f"CREATE DATABASE {q_ident(name)}")

    def drop_database(self, name):
        self.query(f"DROP DATABASE {q_ident(name)}")

    def drop_table(self, db, table):
        self.query(f"DROP TABLE {q_ident(db)}.{q_ident(table)}")

    def copy_to(self, dst, db, table=None, dst_db=None):
        sql = self.dump(db, table)
        target = dst_db or db
        dst.query(f"CREATE DATABASE IF NOT EXISTS {q_ident(target)}")
        dst.load(sql, target)


# ---------------------------------------------------------------------------
class _DBPane(Gtk.Box):
    NAME, ROWS, SIZE, PCT, BYTES, KIND = range(6)

    def __init__(self, commander, endpoint, side):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.commander, self.endpoint, self.side = commander, endpoint, side
        self.level_db = None   # None = databases; else tables in this db

        self.header = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
        self.header.set_tooltip_text("Click to change this connection")
        self.header.connect("clicked", lambda b: commander.connect_endpoint(self))
        self.pack_start(self.header, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, int, GObject.TYPE_INT64, str)
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_grid_lines(Gtk.TreeViewGridLines.VERTICAL)
        c0 = Gtk.TreeViewColumn("Name", pad(Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)), text=0)
        c0.set_expand(True); c0.set_resizable(True); self.view.append_column(c0)
        rr = pad(Gtk.CellRendererText()); rr.set_property("xalign", 1.0)
        c1 = Gtk.TreeViewColumn("Rows", rr, text=1); c1.set_alignment(1.0); self.view.append_column(c1)
        c2 = Gtk.TreeViewColumn("Size", Gtk.CellRendererProgress(), value=3, text=2)
        c2.set_min_width(150); self.view.append_column(c2)
        stripe(self.view)
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.view.connect("row-activated", self._activated)
        sw = Gtk.ScrolledWindow(); sw.add(self.view)
        self.pack_start(sw, True, True, 0)
        self._set_header()

    def _set_header(self):
        ep = self.endpoint
        crumb = f"  ›  {self.level_db}" if self.level_db else ""
        state = "" if ep.available is True else ("  (click to connect)" if ep.available in (False, "auth", None) else "")
        self.header.set_label(f"{ep.label}: {ep.target()}{crumb}{state}")

    def reload(self):
        self._set_header()
        ep = self.endpoint
        self.store.clear()
        if ep.available is not True:
            return
        def work():
            if self.level_db is None:
                items = [(n, None, int(b or 0), "db") for n, b in ep.databases()]
            else:
                items = [(n, int(r or 0), int(b or 0), "table") for n, r, b in ep.tables(self.level_db)]
            GLib.idle_add(self._fill, items)
        self.commander._bg(work)

    def _fill(self, items):
        self.store.clear()
        if self.level_db is not None:
            self.store.append(["..", "", "", 0, 0, "up"])
        maxb = max([b for _, _, b, _ in items], default=0)
        for name, rows, nbytes, kind in items:
            pct = int(round(nbytes * 100.0 / maxb)) if maxb else 0
            self.store.append([name, f"{rows:,}" if rows is not None else "",
                               _human_bytes(nbytes), pct, nbytes, kind])
        total = sum(b for _, _, b, _ in items)
        where = self.level_db or "databases"
        self.commander.status.set_text(
            f"{self.endpoint.label}: {len(items)} {'tables' if self.level_db else 'databases'} "
            f"in {where} · {_human_bytes(total)}")

    def selected(self):
        model, rows = self.view.get_selection().get_selected_rows()
        return [(model[r][self.NAME], model[r][self.KIND]) for r in rows
                if model[r][self.KIND] in ("db", "table")]

    def _activated(self, view, path, col):
        name, kind = self.store[path][self.NAME], self.store[path][self.KIND]
        if kind == "up":
            self.level_db = None; self.reload()
        elif kind == "db":
            self.level_db = name; self.reload()
        elif kind == "table":
            self.commander.view_table(self.endpoint, self.level_db, name)

    def go_up(self):
        if self.level_db is not None:
            self.level_db = None; self.reload()


# ---------------------------------------------------------------------------
class DBCommander(Gtk.Window):
    def __init__(self, conn, parent=None):
        super().__init__(title=f"Databases — {conn.host}")
        self.conn = conn
        w, h = config["window"].get("dbc", (1100, 640))
        self.set_default_size(w, h)
        if parent:
            self.set_transient_for(parent)
        self.connect("delete-event", self._on_close)
        close_on_escape(self)
        self.connect("key-press-event", self._on_key)

        # Both panes start on the tab's SSH host; either can be re-pointed at
        # any other host (or localhost). If a mysql client is already running
        # in the terminal, mirror its -u/-h/-P onto the right pane.
        self.left_ep = MySQLEndpoint(conn.host, conn=conn)
        self.right_ep = MySQLEndpoint(conn.host, conn=conn)
        fg = conn.foreground_process()
        if fg and fg[1] in ("mysql", "mariadb"):
            self.right_ep.opts = _parse_mysql_argv(fg[2])
        _apply_saved_login(self.left_ep)
        _apply_saved_login(self.right_ep)

        self.left = _DBPane(self, self.left_ep, "left")
        self.right = _DBPane(self, self.right_ep, "right")
        self._active = self.left
        for pane in (self.left, self.right):
            pane.view.connect("focus-in-event",
                              lambda w, e, p=pane: (setattr(self, "_active", p), False)[1])

        paned = Gtk.Paned()
        paned.pack1(self.left, True, False)
        paned.pack2(self.right, True, False)
        paned.set_position(config["window"].get("dbc_paned", w // 2))
        self.paned = paned

        self.progress = Gtk.ProgressBar(show_text=True)
        self.status = Gtk.Label(xalign=0); self.status.set_ellipsize(Pango.EllipsizeMode.END)
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vb.pack_start(paned, True, True, 0)
        vb.pack_start(self.progress, False, False, 0)
        vb.pack_start(self.status, False, False, 0)
        vb.pack_start(self._hint_bar(), False, False, 0)
        self.add(vb)

        self._owned_conns = []   # SSH connections opened for panes (not the tab's)
        self._bg(self._startup)

    # ---- infra ---------------------------------------------------------
    def _bg(self, fn, *a):
        def work():
            try:
                fn(*a)
            except Exception as e:
                GLib.idle_add(self.status.set_text, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _startup(self):
        for ep, pane in ((self.left_ep, self.left), (self.right_ep, self.right)):
            ep.probe()
            if ep.available is True:
                GLib.idle_add(pane.reload)
            elif ep.available == "auth":
                GLib.idle_add(self.connect_endpoint, pane)
            else:
                GLib.idle_add(pane._set_header)

    def _hint_bar(self):
        bar = Gtk.Box(spacing=2)
        items = (("Esc", "Close", self.close),
                 ("Tab", "Switch", self._switch_sides),
                 ("Enter", "Open", lambda: None),
                 ("F3", "View", self._view_active),
                 ("F5", "Copy", self.copy_active),
                 ("F6", "Move", self.move_active),
                 ("F7", "Create DB", self.create_db),
                 ("F8", "Drop", self.drop_active))
        for key, label, cb in items:
            b = Gtk.Button(relief=Gtk.ReliefStyle.NONE); b.set_can_focus(False)
            lbl = Gtk.Label(); lbl.set_markup(f"<b>{key}</b> {label}"); b.add(lbl)
            b.connect("clicked", lambda _b, cb=cb: cb())
            bar.pack_start(b, True, True, 0)
        return bar

    def _switch_sides(self):
        target = self.right if self._active is self.left else self.left
        target.view.grab_focus(); self._active = target

    def _on_key(self, w, ev):
        k = ev.keyval
        if k in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab): self._switch_sides(); return True
        if k == Gdk.KEY_BackSpace: self._active.go_up(); return True
        if k == Gdk.KEY_F3: self._view_active(); return True
        if k == Gdk.KEY_F5: self.copy_active(); return True
        if k == Gdk.KEY_F6: self.move_active(); return True
        if k == Gdk.KEY_F7: self.create_db(); return True
        if k == Gdk.KEY_F8: self.drop_active(); return True
        return False

    def _on_close(self, *_):
        config["window"]["dbc"] = list(self.get_size())
        config["window"]["dbc_paned"] = self.paned.get_position()
        config.save()
        for c in self._owned_conns:
            try: c.close()
            except Exception: pass
        return False

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

    def _progress(self, frac, text):
        self.progress.set_fraction(min(frac, 1.0)); self.progress.set_text(text)

    # ---- connect / credentials -----------------------------------------
    def connect_endpoint(self, pane):
        """Point a pane at any SSH host (mysql runs there over the session)."""
        ep = pane.endpoint
        from .app import ssh_config_hosts
        d = Gtk.Dialog(title=f"Connect — {pane.side} pane", transient_for=self, flags=0)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Connect", Gtk.ResponseType.OK)
        d.set_default_size(600, 400)
        box = d.get_content_area(); box.set_spacing(6)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(10)

        body = Gtk.Box(spacing=10)
        store = Gtk.ListStore(str, str, str, int)
        for h in sorted(config["hosts"], key=lambda h: (h.get("name") or h["host"]).lower()):
            store.append([h.get("name") or h["host"], h["host"], h.get("user", ""), int(h.get("port") or 22)])
        for h in ssh_config_hosts():
            store.append([h["name"] + "  (ssh config)", h["host"], h["user"], h["port"]])
        hv = Gtk.TreeView(model=store)
        hv.append_column(Gtk.TreeViewColumn("SSH hosts", Gtk.CellRendererText(), text=0))
        hsw = Gtk.ScrolledWindow(); hsw.add(hv); hsw.set_size_request(220, 210)
        body.pack_start(hsw, False, False, 0)

        grid = Gtk.Grid(row_spacing=6, column_spacing=6)
        fields = {}
        for i, (k, lab) in enumerate((("host", "SSH Host"), ("user", "SSH User"), ("port", "SSH Port"),
                                      ("myuser", "MySQL user (opt)"), ("mypass", "MySQL password (opt)"))):
            grid.attach(Gtk.Label(label=lab, xalign=1), 0, i, 1, 1)
            e = Gtk.Entry()
            if k == "mypass":
                e.set_visibility(False)
            fields[k] = e; grid.attach(e, 1, i, 1, 1)
        body.pack_start(grid, True, True, 0)
        box.add(body)

        def on_sel(sel):
            m, it = sel.get_selected()
            if it:
                fields["host"].set_text(m[it][1]); fields["user"].set_text(m[it][2])
                fields["port"].set_text("" if m[it][3] == 22 else str(m[it][3]))
        hv.get_selection().connect("changed", on_sel)

        hint = Gtk.Label(xalign=0, wrap=True); hint.get_style_context().add_class("dim-label")
        hint.set_text("Opens a hidden SSH session and runs mysql on that host (socket auth) — "
                      "leave MySQL user blank to use the box's default (root when you SSH as root). "
                      "Point a pane at localhost to reach this machine's own server.")
        box.add(hint)
        remember = Gtk.CheckButton(label="Remember this login")
        remember.set_active(_login_key(ep) in config.get("db_logins", {}))
        box.add(remember)
        if ep.last_error:
            err = Gtk.Label(xalign=0, wrap=True)
            err.set_markup(f"<span foreground='#d9534f' size='small'>{GLib.markup_escape_text(ep.last_error)}</span>")
            box.add(err)

        d.set_default_response(Gtk.ResponseType.OK); d.show_all()
        ok = d.run() == Gtk.ResponseType.OK
        v = {k: e.get_text().strip() for k, e in fields.items()}
        rem = remember.get_active()
        d.destroy()
        if not ok:
            return
        if v["host"]:
            self._apply_ssh(pane, v["host"], v["user"], v["port"], v["myuser"], v["mypass"], rem)
        else:
            self.status.set_text("Select or enter an SSH host")

    def _apply_ssh(self, pane, host, user, port, myuser, mypass, remember, sshpass=None):
        GLib.idle_add(self.status.set_text, f"Connecting to {host}…")
        def work():
            try:
                conn = SSHConnection(host, user=user or None,
                                     port=int(port) if port else None, password=sshpass)
            except paramiko.AuthenticationException:
                GLib.idle_add(self._retry_ssh, pane, host, user, port, myuser, mypass, remember)
                return
            except Exception as e:
                GLib.idle_add(self.status.set_text, f"SSH {host}: {e}")
                return
            self._owned_conns.append(conn)
            ep = pane.endpoint
            ep.conn = conn; ep.label = host
            ep.opts = {"user": myuser or None, "host": None, "port": None, "db": None, "defaults": []}
            ep.password = mypass or None
            _store_login(ep, remember)
            pane.level_db = None
            ep.probe()
            if ep.available is True:
                GLib.idle_add(pane.reload)
            else:
                GLib.idle_add(self.status.set_text, f"{host}: {ep.last_error or 'MySQL not reachable on this host'}")
                GLib.idle_add(pane._set_header)
        self._bg(work)

    def _retry_ssh(self, pane, host, user, port, myuser, mypass, remember):
        pw = self._ask_password(f"SSH password for {user or ''}@{host}:")
        if pw is not None:
            self._apply_ssh(pane, host, user, port, myuser, mypass, remember, sshpass=pw)

    def confirm(self, text):
        md = Gtk.MessageDialog(transient_for=self, message_type=Gtk.MessageType.QUESTION,
                               buttons=Gtk.ButtonsType.OK_CANCEL, text=text)
        ok = md.run() == Gtk.ResponseType.OK; md.destroy(); return ok

    # ---- object operations ---------------------------------------------
    def _view_active(self):
        pane = self._active
        sel = pane.selected()
        tables = [n for n, k in sel if k == "table"]
        if tables and pane.level_db:
            self.view_table(pane.endpoint, pane.level_db, tables[0])

    def view_table(self, endpoint, db, table):
        win = Gtk.Window(title=f"{endpoint.label}: {db}.{table}")
        win.set_default_size(760, 480); win.set_transient_for(self)
        close_on_escape(win)
        sw = Gtk.ScrolledWindow(); win.add(sw); win.show_all()
        def work():
            cols, rows = endpoint.query(f"SELECT * FROM {q_ident(table)} LIMIT 500", db=db)
            GLib.idle_add(self._fill_rows, sw, cols, rows)
        self._bg(work)

    def _fill_rows(self, sw, cols, rows):
        if not cols:
            return
        store = Gtk.ListStore(*([str] * len(cols)))
        for r in rows:
            store.append((r + [""] * len(cols))[:len(cols)])
        tv = Gtk.TreeView(model=store)
        tv.set_grid_lines(Gtk.TreeViewGridLines.VERTICAL)
        for i, c in enumerate(cols):
            col = Gtk.TreeViewColumn(c.replace("_", "__"), pad(Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)), text=i)
            col.set_resizable(True); tv.append_column(col)
        stripe(tv)
        sw.add(tv); tv.show_all()

    def copy_active(self, move=False):
        src = self._active
        dst = self.right if src is self.left else self.left
        if dst.endpoint.available is not True:
            self.status.set_text(f"{dst.endpoint.label} side isn't connected"); return
        items = src.selected()
        if not items:
            return
        db = src.level_db
        verb = "Move" if move else "Copy"
        if src.level_db:                       # copying tables
            into = dst.level_db or src.level_db
            dest_desc = f"{dst.endpoint.label} database `{into}`"
        else:                                  # copying whole databases (keep names)
            into = None
            dest_desc = f"{dst.endpoint.label} ({dst.endpoint.target()})"
        what = ", ".join(n for n, _ in items[:4]) + (" …" if len(items) > 4 else "")
        if not self.confirm(f"{verb} {len(items)} object(s) to {dest_desc}?\n{what}"):
            return

        def work():
            done = 0
            for name, kind in items:
                GLib.idle_add(self._progress, done / len(items), f"{verb.lower()}ing {name}…")
                if kind == "db":
                    src.endpoint.copy_to(dst.endpoint, name)
                    if move:
                        src.endpoint.drop_database(name)
                else:  # table -> the destination pane's current database
                    src.endpoint.copy_to(dst.endpoint, db, name, dst_db=into)
                    if move:
                        src.endpoint.drop_table(db, name)
                done += 1
            GLib.idle_add(self._progress, 1.0, f"{verb}d {done} object(s)")
            GLib.idle_add(dst.reload)
            if move:
                GLib.idle_add(src.reload)
        self._bg(work)

    def move_active(self):
        self.copy_active(move=True)

    def create_db(self):
        pane = self._active
        if pane.endpoint.available is not True:
            return
        d = Gtk.Dialog(title=f"Create database on {pane.endpoint.label}", transient_for=self, flags=0)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create", Gtk.ResponseType.OK)
        box = d.get_content_area(); box.set_margin_start(12); box.set_margin_end(12); box.set_spacing(6)
        box.add(Gtk.Label(label="Database name:", xalign=0))
        e = Gtk.Entry(activates_default=True); box.add(e)
        d.set_default_response(Gtk.ResponseType.OK); d.show_all()
        name = e.get_text().strip() if d.run() == Gtk.ResponseType.OK else None
        d.destroy()
        if name:
            self._bg(lambda: (pane.endpoint.create_database(name), GLib.idle_add(pane.reload)))

    def drop_active(self):
        pane = self._active
        items = pane.selected()
        if not items:
            return
        db = pane.level_db
        what = ", ".join(n for n, _ in items[:5]) + (" …" if len(items) > 5 else "")
        kind = "table(s)" if db else "DATABASE(S)"
        if not self.confirm(f"Drop {len(items)} {kind} on {pane.endpoint.label}?\n{what}\n\nThis cannot be undone."):
            return
        def work():
            for name, k in items:
                if k == "db":
                    pane.endpoint.drop_database(name)
                else:
                    pane.endpoint.drop_table(db, name)
            GLib.idle_add(pane.reload)
        self._bg(work)
