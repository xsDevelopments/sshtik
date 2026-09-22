"""Norton-Commander-style MySQL browser: an SSH host's server on each side,
with copy/move of tables and databases between them. Not a HeidiSQL clone —
a transfer tool that speaks MySQL.

Each pane is a MySQLEndpoint that runs mysql/mysqldump on its SSH host over an
exec channel (socket auth on that box, so no per-host TCP grants are needed).
Either pane can be re-pointed at any host in your SSH config — including
localhost, to reach this machine's own server. F5 copies the highlighted table
or database to the other side (mysqldump | mysql); F6 moves it."""
import re
import shlex
import threading

import paramiko

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango, GObject

from .config import config
from .connection import SSHConnection
from .ui import stripe, close_on_escape, pad
from .dbpanel import q_ident, q_val, _human_bytes, _parse_mysql_argv, AuthError, NULL


# Row icons for the database list. Icon themes disagree on which names they
# ship, so each kind is a preference chain; _resolve_icon returns the first the
# current theme actually has (the file panel does the same for file types). The
# classic cylinder (x-office-database) stands in for a database, a spreadsheet
# grid for a table.
# "db" prefers the canonical database cylinder (x-office-database, present in
# Adwaita/the Flatpak); where a theme lacks it, a platter-stack disk icon is the
# closest cylinder before falling back to the flat SQL-page glyph.
_ICON_CHAINS = {
    "db":    ("x-office-database", "drive-multidisk", "drive-harddisk", "application-sql"),
    "table": ("x-office-spreadsheet", "x-office-database", "table", "text-x-generic"),
    "up":    ("go-up", "go-previous", "folder"),
}
_icon_cache = {}


def _resolve_icon(kind):
    """First icon name in the kind's chain that the theme actually has (cached)."""
    name = _icon_cache.get(kind)
    if name is None:
        chain = _ICON_CHAINS[kind]
        try:
            theme = Gtk.IconTheme.get_default()
            name = next((n for n in chain if theme.has_icon(n)), chain[-1])
        except Exception:
            name = chain[0]
        _icon_cache[kind] = name
    return name


# TEXT/BLOB columns can hold tabs, newlines or embedded HTML. Fetching rows with
# `mysql --batch` (no --raw) escapes control characters, so a stray newline or
# tab inside a value can no longer split one row into several or shift columns.
# _batch_unescape turns those escapes back into the real value for the detail
# form; _cell_display shows a cleaned, single-line, length-capped version in the
# grid so one giant TEXT field can't wreck the layout.
_ESCAPES = {"0": "\0", "t": "\t", "n": "\n", "r": "\r", "\\": "\\"}


def _batch_unescape(s):
    if "\\" not in s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            out.append(_ESCAPES.get(s[i + 1], s[i + 1])); i += 2
        else:
            out.append(c); i += 1
    return "".join(out)


def _cell_display(s, limit=200):
    s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return s if len(s) <= limit else s[:limit] + "…"


_TEXT_TYPES = {"text", "tinytext", "mediumtext", "longtext", "json",
               "blob", "tinyblob", "mediumblob", "longblob"}


def _enum_values(column_type):
    """Pull the options out of an enum('a','b') / set('x','y') column type."""
    return [m.group(1).replace("''", "'")
            for m in re.finditer(r"'((?:[^']|'')*)'", column_type or "")]


def _is_longtext(m):
    """A column that holds long, possibly multi-line prose: a TEXT/BLOB type, or
    a roomy varchar/char (>=256). These cap short in the grid and open a
    multi-line editor in the form."""
    dt = m.get("data_type", "")
    if dt in _TEXT_TYPES:
        return True
    if dt in ("varchar", "char", "varbinary", "binary"):
        mo = re.search(r"\((\d+)\)", m.get("column_type", ""))
        return bool(mo) and int(mo.group(1)) >= 256
    return False

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

    def query(self, sql, db=None, raw=True):
        argv = ["mysql", "--batch"] + (["--raw"] if raw else []) + self._conn_args()
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
        if raw:
            lines = out.splitlines()
        else:
            # --batch escapes LF and TAB but NOT carriage return, so split rows
            # on the LF terminator only. str.splitlines() also breaks on a bare
            # CR (0x0D), which shreds one row carrying CRLF text into several.
            lines = out.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
        rows = [line.split("\t") for line in lines]
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

    def columns(self, db, table):
        """Column metadata for a table, in definition order — used to build the
        row detail form (widget per type, primary key, nullability)."""
        q = ("SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, COLUMN_KEY, "
             "IS_NULLABLE, EXTRA FROM information_schema.COLUMNS "
             f"WHERE TABLE_SCHEMA={q_val(db)} AND TABLE_NAME={q_val(table)} "
             "ORDER BY ORDINAL_POSITION")
        _, rows = self.query(q, db=db)
        cols = []
        for r in rows:
            r = (r + [""] * 6)[:6]
            cols.append({"name": r[0], "data_type": r[1].lower(),
                         "column_type": r[2], "key": r[3],
                         "nullable": r[4] == "YES", "extra": r[5].lower()})
        return cols

    def table_stats(self, db, table):
        """Exact row count and on-disk size for the grid's footer."""
        cnt = size = 0
        try:
            _, cr = self.query(f"SELECT COUNT(*) FROM {q_ident(table)}", db=db)
            if cr and cr[0]:
                cnt = int(cr[0][0] or 0)
        except Exception:
            pass
        try:
            _, sr = self.query(
                "SELECT COALESCE(DATA_LENGTH+INDEX_LENGTH,0) FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA={q_val(db)} AND TABLE_NAME={q_val(table)}", db=db)
            if sr and sr[0]:
                size = int(sr[0][0] or 0)
        except Exception:
            pass
        return cnt, size

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
    NAME, ROWS, SIZE, PCT, BYTES, KIND, ICON = range(7)

    def __init__(self, commander, endpoint, side):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.get_style_context().add_class("sshtik-pane")
        self.commander, self.endpoint, self.side = commander, endpoint, side
        self.level_db = None   # None = databases; else tables in this db
        self.items = []
        self.sort_key = None    # None | "name" | "rows" | "size"
        self.sort_desc = False

        self.header = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
        self.header.set_tooltip_text("Click to change this connection")
        self.header.connect("clicked", lambda b: commander.connect_endpoint(self))
        self.pack_start(self.header, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, int, GObject.TYPE_INT64, str, str)
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_grid_lines(Gtk.TreeViewGridLines.VERTICAL)
        # Name column: a database/table icon, then the name.
        c0 = Gtk.TreeViewColumn("Name")
        icon_rend = Gtk.CellRendererPixbuf(); icon_rend.set_property("xpad", 4)
        c0.pack_start(icon_rend, False)
        c0.add_attribute(icon_rend, "icon-name", self.ICON)
        name_rend = pad(Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END))
        c0.pack_start(name_rend, True)
        c0.add_attribute(name_rend, "text", self.NAME)
        c0.set_expand(True); c0.set_resizable(True); self.view.append_column(c0)
        rr = pad(Gtk.CellRendererText()); rr.set_property("xalign", 1.0)
        c1 = Gtk.TreeViewColumn("Rows", rr, text=1); c1.set_alignment(1.0); self.view.append_column(c1)
        c2 = Gtk.TreeViewColumn("Size", Gtk.CellRendererProgress(), value=3, text=2)
        c2.set_min_width(150); self.view.append_column(c2)
        # Manual 3-state sort (off -> ascending -> descending -> off) with the
        # arrow inverted (down = largest/most rows first), matching the file panel.
        self.columns = {"name": c0, "rows": c1, "size": c2}
        for key, col in self.columns.items():
            col.set_clickable(True)
            col.connect("clicked", lambda c, k=key: self._sort_clicked(k))
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

    def reload(self, select="..", focus=False):
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
            GLib.idle_add(self._fill, items, select, focus)
        self.commander._bg(work)

    _SORT_KEYS = {
        "name": lambda it: it[0].lower(),
        "rows": lambda it: it[1] or 0,
        "size": lambda it: it[2],
    }

    def _sorted_items(self):
        if self.sort_key is None:           # default: the SQL order (name A-Z)
            return self.items
        return sorted(self.items, key=self._SORT_KEYS[self.sort_key], reverse=self.sort_desc)

    def _render(self):
        self.store.clear()
        if self.level_db is not None:
            self.store.append(["..", "", "", 0, 0, "up", _resolve_icon("up")])
        items = self._sorted_items()
        maxb = max([b for _, _, b, _ in items], default=0)
        for name, rows, nbytes, kind in items:
            pct = int(round(nbytes * 100.0 / maxb)) if maxb else 0
            self.store.append([name, f"{rows:,}" if rows is not None else "",
                               _human_bytes(nbytes), pct, nbytes, kind,
                               _resolve_icon("db" if kind == "db" else "table")])

    def _fill(self, items, select="..", focus=False):
        self.items = items
        self._render()
        total = sum(b for _, _, b, _ in items)
        where = self.level_db or "databases"
        self.commander.status.set_text(
            f"{self.endpoint.label}: {len(items)} {'tables' if self.level_db else 'databases'} "
            f"in {where} · {_human_bytes(total)}")
        self._select_name(select, focus)

    def _select_name(self, name, focus=True):
        """Cursor/selection onto the row `name` (falling back to the top). With
        focus, grab the list so it reads blue; else just select it."""
        if len(self.store) == 0:
            return
        idx = next((i for i, r in enumerate(self.store) if r[self.NAME] == name), 0)
        p = Gtk.TreePath(idx)
        if focus:
            self.view.set_cursor(p)
        else:
            sel = self.view.get_selection()
            sel.unselect_all(); sel.select_path(p)
        self.view.scroll_to_cell(p, None, False, 0, 0)

    def _sort_clicked(self, key):
        """Cycle one column: off -> ascending -> descending -> off."""
        if self.sort_key != key:
            self.sort_key, self.sort_desc = key, False
        elif not self.sort_desc:
            self.sort_desc = True
        else:
            self.sort_key = None
        self._update_sort_indicators()
        self._render()

    def _update_sort_indicators(self):
        for k, col in self.columns.items():
            active = k == self.sort_key
            col.set_sort_indicator(active)
            if active:                       # invert: down = largest/most first
                col.set_sort_order(Gtk.SortType.ASCENDING if self.sort_desc
                                   else Gtk.SortType.DESCENDING)

    def selected(self):
        model, rows = self.view.get_selection().get_selected_rows()
        return [(model[r][self.NAME], model[r][self.KIND]) for r in rows
                if model[r][self.KIND] in ("db", "table")]

    def _activated(self, view, path, col):
        name, kind = self.store[path][self.NAME], self.store[path][self.KIND]
        if kind == "up":
            self.go_up()
        elif kind == "db":                   # into a database: land on ".."
            self.level_db = name; self.reload(select="..", focus=True)
        elif kind == "table":
            self.commander.view_table(self.endpoint, self.level_db, name)

    def go_up(self):
        if self.level_db is not None:        # back to the database list, on the db just left
            left = self.level_db
            self.level_db = None
            self.reload(select=left, focus=True)


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
            pane.view.connect(
                "focus-in-event", lambda w, e, p=pane: (self._focus_pane(p, True), False)[1])
            pane.header.connect(
                "focus-in-event", lambda w, e, p=pane: (self._focus_pane(p, False), False)[1])
        self._focus_pane(self.left, True)

        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        mid.set_valign(Gtk.Align.CENTER)
        to_right = Gtk.Button(label="→"); to_right.set_tooltip_text("Copy left → right")
        to_right.set_can_focus(False)
        to_right.connect("clicked", lambda b: self._copy_between(self.left))
        to_left = Gtk.Button(label="←"); to_left.set_tooltip_text("Copy right → left")
        to_left.set_can_focus(False)
        to_left.connect("clicked", lambda b: self._copy_between(self.right))
        mid.pack_start(to_right, False, False, 0); mid.pack_start(to_left, False, False, 0)

        paned = Gtk.Paned()
        paned.pack1(self.left, True, False)
        rbox = Gtk.Box(spacing=4)
        rbox.pack_start(mid, False, False, 0)
        rbox.pack_start(self.right, True, True, 0)
        paned.pack2(rbox, True, False)
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
        GLib.idle_add(self.left.view.grab_focus)

    # ---- infra ---------------------------------------------------------
    def _bg(self, fn, *a):
        def work():
            try:
                fn(*a)
            except Exception as e:
                GLib.idle_add(self.status.set_text, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _startup(self):
        panes = [(self.left_ep, self.left), (self.right_ep, self.right)]
        # A pinned "home" host (other than the tab's own) auto-connects the
        # left pane; the right pane always follows the tab's SSH session.
        dflt = config.get("db_default_host", {})
        if dflt.get("host") and dflt["host"] != self.conn.host:
            login = config.get("db_logins", {}).get(f"ssh:{dflt['host']}") or {}
            GLib.idle_add(self._apply_ssh, self.left, dflt["host"],
                          dflt.get("user") or "", str(dflt.get("port") or ""),
                          login.get("user") or "", login.get("password") or "",
                          f"ssh:{dflt['host']}" in config.get("db_logins", {}))
            panes = panes[1:]
        for ep, pane in panes:
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

    def _focus_pane(self, pane, is_list):
        """One blue highlight = where focus is: the pane whose list has focus
        shows its selection accent; every other selection is muted grey."""
        self._active = pane
        for p in (self.left, self.right):
            sc = p.get_style_context()
            if is_list and p is pane:
                sc.add_class("list-focused")
            else:
                sc.remove_class("list-focused")

    def _switch_sides(self):
        target = self.right if self._active is self.left else self.left
        target.view.grab_focus(); self._focus_pane(target, True)

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
        d.add_buttons("Connect", Gtk.ResponseType.OK, "Cancel", Gtk.ResponseType.CANCEL)
        d.set_default_size(600, 400)
        box = d.get_content_area(); box.set_spacing(6)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(10)

        # The left pane can pin a "home" host it auto-connects to on every open.
        dflt_host = config.get("db_default_host", {}).get("host") if pane.side == "left" else None

        body = Gtk.Box(spacing=10)
        store = Gtk.ListStore(str, str, str, int)
        def _label(name, host):
            return name + ("  (default)" if host == dflt_host else "")
        for h in sorted(config["hosts"], key=lambda h: (h.get("name") or h["host"]).lower()):
            store.append([_label(h.get("name") or h["host"], h["host"]), h["host"], h.get("user", ""), int(h.get("port") or 22)])
        for h in ssh_config_hosts():
            store.append([_label(h["name"] + "  (ssh config)", h["host"]), h["host"], h["user"], h["port"]])
        hv = Gtk.TreeView(model=store)
        hv.append_column(Gtk.TreeViewColumn("SSH hosts", Gtk.CellRendererText(), text=0))
        hsw = Gtk.ScrolledWindow(); hsw.add(hv); hsw.set_size_request(220, 210)
        body.pack_start(hsw, False, False, 0)

        grid = Gtk.Grid(row_spacing=6, column_spacing=6)
        fields = {}
        specs = (("host", "SSH Host"), ("user", "SSH User"), ("port", "SSH Port"),
                 ("myuser", "MySQL user (opt)"), ("mypass", "MySQL password (opt)"))
        for i, (k, lab) in enumerate(specs):
            grid.attach(Gtk.Label(label=lab, xalign=1), 0, i, 1, 1)
            e = Gtk.Entry()
            if k == "mypass":
                e.set_visibility(False)
            fields[k] = e; grid.attach(e, 1, i, 1, 1)
        # Start on the pane's current host so the default checkbox reads true.
        fields["host"].set_text(ep.conn.host)
        default_chk = None
        if pane.side == "left":
            default_chk = Gtk.CheckButton(label="Default left pane")
            default_chk.set_tooltip_text(
                "Auto-connect the left pane to this host every time the database window opens")
            default_chk.set_active(bool(dflt_host) and dflt_host == ep.conn.host)
            grid.attach(default_chk, 1, len(specs), 1, 1)
        body.pack_start(grid, True, True, 0)
        box.add(body)

        def on_sel(sel):
            m, it = sel.get_selected()
            if it:
                fields["host"].set_text(m[it][1]); fields["user"].set_text(m[it][2])
                fields["port"].set_text("" if m[it][3] == 22 else str(m[it][3]))
                if default_chk is not None:     # only stays ticked on the pinned host
                    default_chk.set_active(m[it][1] == dflt_host)
        hv.get_selection().connect("changed", on_sel)
        for i, r in enumerate(store):       # pre-select the current host's row if listed
            if r[1] == ep.conn.host:
                hv.get_selection().select_path(Gtk.TreePath(i)); break

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
        make_default = bool(default_chk and default_chk.get_active())
        d.destroy()
        if not ok:
            return
        if not v["host"]:
            self.status.set_text("Select or enter an SSH host"); return
        if default_chk is not None:         # left pane: update the pinned default
            cur = config.get("db_default_host", {}).get("host")
            if make_default:
                config["db_default_host"] = {"host": v["host"], "user": v["user"], "port": v["port"]}
                config.save()
            elif cur == v["host"]:
                config["db_default_host"] = {}; config.save()
        self._apply_ssh(pane, v["host"], v["user"], v["port"], v["myuser"], v["mypass"], rem)

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
        win.set_default_size(820, 520); win.set_transient_for(self)
        close_on_escape(win)
        sw = Gtk.ScrolledWindow()
        info = Gtk.Label(xalign=0); info.set_ellipsize(Pango.EllipsizeMode.END)
        newb = Gtk.Button(label="New Record"); backb = Gtk.Button(label="Back")
        bar = Gtk.Box(spacing=6)
        for side in ("start", "end", "top", "bottom"):
            getattr(bar, f"set_margin_{side}")(6)
        bar.pack_start(info, True, True, 4)          # bottom-left: totals
        bar.pack_start(newb, False, False, 0)        # bottom-right: New Record · Back
        bar.pack_start(backb, False, False, 0)
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        vb.pack_start(sw, True, True, 0)
        vb.pack_start(bar, False, False, 0)
        win.add(vb); win.show_all()

        ctx = {"endpoint": endpoint, "db": db, "table": table, "win": win,
               "sw": sw, "info": info, "cols": [], "meta": []}
        newb.connect("clicked", lambda b: self._row_form(ctx, None, None, None))
        backb.connect("clicked", lambda b: win.destroy())
        self._reload_table(ctx)

    def _reload_table(self, ctx):
        endpoint, db, table = ctx["endpoint"], ctx["db"], ctx["table"]

        def work():
            cols, rows = endpoint.query(
                f"SELECT * FROM {q_ident(table)} LIMIT 500", db=db, raw=False)
            try:
                meta = endpoint.columns(db, table)
            except Exception:
                meta = []
            cnt, size = endpoint.table_stats(db, table)
            GLib.idle_add(self._fill_rows, ctx, cols, rows, meta, cnt, size)
        self._bg(work)

    def _fill_rows(self, ctx, cols, rows, meta, cnt, size):
        ctx["cols"], ctx["meta"] = cols, meta
        db, table, sw = ctx["db"], ctx["table"], ctx["sw"]
        shown = f"  (showing first {len(rows)})" if cnt > len(rows) else ""
        ctx["info"].set_text(
            f"Total records in {db} › {table} : {cnt:,} · {_human_bytes(size)}{shown}")
        if not cols:
            for ch in sw.get_children():
                sw.remove(ch)
            return
        # Real values (un-escaped) drive the detail form; the grid shows a
        # cleaned, single-line, length-capped copy so embedded HTML/newlines
        # can't break the table structure. TEXT/BLOB columns cap short (40) —
        # they hold the long, multi-line, CRLF-laden content; open the row to
        # read the whole thing.
        by_name = {m["name"]: m for m in meta}
        limits = [40 if _is_longtext(by_name.get(c, {})) else 200 for c in cols]
        ctx["limits"] = limits
        real_rows = [[_batch_unescape(v) for v in (r + [""] * len(cols))[:len(cols)]]
                     for r in rows]
        store = Gtk.ListStore(*([str] * len(cols)))
        for rr in real_rows:
            store.append([_cell_display(v, limits[i]) for i, v in enumerate(rr)])
        tv = Gtk.TreeView(model=store)
        tv.set_grid_lines(Gtk.TreeViewGridLines.VERTICAL)
        tv.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        # Flex each column to fit its widest cell, clamped so thin columns stay
        # legible and one big TEXT column can't blow out the grid.
        sample = real_rows[:60]
        for i, c in enumerate(cols):
            longest = max([len(c)] + [len(_cell_display(r[i], limits[i])) for r in sample])
            width = max(70, min(20 + longest * 8, 420))
            col = Gtk.TreeViewColumn(c.replace("_", "__"),
                                     pad(Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)), text=i)
            col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            col.set_fixed_width(width); col.set_min_width(48); col.set_resizable(True)
            tv.append_column(col)
        stripe(tv)
        # Double-click or Enter on a row opens it as a form.
        tv.connect("row-activated", lambda v, p, c: self._row_form(
            ctx, real_rows, store, p.get_indices()[0]))
        for ch in sw.get_children():
            sw.remove(ch)
        sw.add(tv); tv.show_all()

    # ---- single-row detail form ----------------------------------------
    def _field_widget(self, m, val):
        """A form widget for one column, chosen by its type. Returns
        (widget, get, set): get() yields the current string (or None if the
        field is read-only), set(v) restores a value for Revert."""
        dt = m.get("data_type", ""); ct = m.get("column_type", "")
        readonly = "auto_increment" in m.get("extra", "") or "generated" in m.get("extra", "")
        if dt == "enum":
            options = _enum_values(ct)
            combo = Gtk.ComboBoxText(); combo.set_hexpand(True)
            for opt in options:
                combo.append_text(opt)
            if val in options:
                combo.set_active(options.index(val))
            combo.set_sensitive(not readonly)
            return (combo,
                    (lambda: None if readonly else (combo.get_active_text() or "")),
                    (lambda v: combo.set_active(options.index(v) if v in options else -1)))
        if ct == "tinyint(1)":                # MySQL's conventional boolean
            chk = Gtk.CheckButton(); chk.set_active(val not in ("", "0", NULL))
            chk.set_sensitive(not readonly)
            return (chk,
                    (lambda: None if readonly else ("1" if chk.get_active() else "0")),
                    (lambda v: chk.set_active(v not in ("", "0", NULL))))
        if _is_longtext(m):
            tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
            tv.set_editable(not readonly); tv.set_sensitive(not readonly)
            tv.get_buffer().set_text(val)
            box = Gtk.ScrolledWindow(); box.set_size_request(-1, 96); box.set_hexpand(True)
            box.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            box.set_shadow_type(Gtk.ShadowType.IN); box.add(tv)
            def get_tv():
                if readonly:
                    return None
                b = tv.get_buffer(); return b.get_text(*b.get_bounds(), True)
            return box, get_tv, (lambda v: tv.get_buffer().set_text(v))
        e = Gtk.Entry(); e.set_text(val); e.set_hexpand(True)
        e.set_editable(not readonly); e.set_sensitive(not readonly)
        return e, (lambda: None if readonly else e.get_text()), (lambda v: e.set_text(v))

    @staticmethod
    def _row_identity(cols, by_name):
        """Columns that pick out one row for UPDATE/DELETE, tightest first:
        PRIMARY, then UNIQUE, then an auto-increment column (unique in practice
        even when indexed non-uniquely, as MySQL allows). Failing all of those,
        match the whole row on its non-text columns (with LIMIT 1). Returns
        (columns, kind)."""
        pri = [c for c in cols if by_name.get(c, {}).get("key") == "PRI"]
        if pri:
            return pri, "primary"
        uni = [c for c in cols if by_name.get(c, {}).get("key") == "UNI"]
        if uni:
            return uni, "unique"
        auto = [c for c in cols if "auto_increment" in by_name.get(c, {}).get("extra", "")]
        if auto:
            return auto, "auto"
        return [c for c in cols if not _is_longtext(by_name.get(c, {}))], "full"

    @staticmethod
    def _where(ident, orig):
        """WHERE clause matching the row's original values (NULL-safe)."""
        return " AND ".join(
            (f"{q_ident(c)} IS NULL" if orig[c] == NULL else f"{q_ident(c)}={q_val(orig[c])}")
            for c in ident)

    def _row_form(self, ctx, real_rows, store, idx):
        """Open one row as a form — a widget per column. Editing an existing row
        (idx set) offers Save / Revert / Back plus a red Delete; New Record
        (idx None) offers the same minus Delete, and Save inserts."""
        endpoint, db, table = ctx["endpoint"], ctx["db"], ctx["table"]
        meta = ctx["meta"]
        cols = ctx["cols"] or [m["name"] for m in meta]
        is_new = idx is None
        values = None if is_new else real_rows[idx]
        orig = {c: "" for c in cols} if is_new else {c: values[i] for i, c in enumerate(cols)}
        by_name = {m["name"]: m for m in meta}
        pk = [m["name"] for m in meta if m["key"] == "PRI" and m["name"] in cols]
        ident, ident_kind = self._row_identity(cols, by_name)
        # A multi-line editor can't faithfully round-trip a bare CR, so normalise
        # CRLF/CR to LF up front — display is clean and an untouched field won't
        # look "changed". Editing such a field then saves it CR-free (the fix the
        # user is after anyway).
        for c in cols:
            if _is_longtext(by_name.get(c, {})):
                orig[c] = orig[c].replace("\r\n", "\n").replace("\r", "\n")

        win = Gtk.Window(title=f"{endpoint.label}: {db}.{table} — "
                               + ("new record" if is_new else "row"))
        win.set_default_size(480, 540); win.set_transient_for(ctx["win"])
        close_on_escape(win)

        grid = Gtk.Grid(row_spacing=8, column_spacing=10)
        for side in ("start", "end", "top", "bottom"):
            getattr(grid, f"set_margin_{side}")(12)
        getters, setters = {}, {}
        for row_i, c in enumerate(cols):
            m = by_name.get(c, {"data_type": "", "column_type": "", "extra": "",
                                "nullable": True, "key": ""})
            lab = Gtk.Label(xalign=1, yalign=0.0)
            lab.set_max_width_chars(26); lab.set_line_wrap(True)
            lab.set_markup(
                f"<b>{GLib.markup_escape_text(c + ('  (PK)' if c in pk else ''))}</b>\n"
                f"<small><span alpha='55%'>{GLib.markup_escape_text(m.get('column_type') or m.get('data_type') or '')}</span></small>")
            grid.attach(lab, 0, row_i, 1, 1)
            widget, get, setv = self._field_widget(m, orig[c])
            grid.attach(widget, 1, row_i, 1, 1)
            getters[c], setters[c] = get, setv

        gsw = Gtk.ScrolledWindow()
        gsw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        gsw.add(grid)

        if ident_kind == "primary":
            keynote = ""
        elif ident_kind in ("unique", "auto") and ident:
            keynote = f"  No primary key — identifying the row by {', '.join(ident)}."
        elif ident:
            keynote = "  No unique key — updates and deletes match the full row, one at a time."
        else:
            keynote = "  This row has no safe identifier — it can't be saved or deleted."
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.get_style_context().add_class("dim-label")
        hint.set_text(("Empty a nullable field to store NULL; auto-increment keys fill themselves."
                       if is_new else
                       "Only changed fields are written. Empty a nullable field to store NULL.")
                      + ("" if is_new else keynote))
        hint.set_margin_start(12); hint.set_margin_end(12)

        # Delete sits bottom-left (edit only); Save · Revert · Back bottom-right.
        bar = Gtk.Box(spacing=6)
        for side in ("start", "end", "bottom"):
            getattr(bar, f"set_margin_{side}")(10)
        save = Gtk.Button(label="Save"); save.get_style_context().add_class("suggested-action")
        save.set_sensitive(bool(ident) or is_new)
        revert = Gtk.Button(label="Revert"); back = Gtk.Button(label="Back")
        if not is_new:
            delete = Gtk.Button(label="Delete")
            delete.get_style_context().add_class("destructive-action")
            delete.set_sensitive(bool(ident))
            bar.pack_start(delete, False, False, 0)
        bar.pack_end(back, False, False, 0)         # packed end-first -> rightmost
        bar.pack_end(revert, False, False, 0)
        bar.pack_end(save, False, False, 0)         # Save · Revert · Back

        def on_revert(_b):
            for c in cols:
                setters[c](orig[c])
            self.status.set_text("Reverted")

        def on_save(_b):
            if is_new:
                names, vals = [], []
                for c in cols:
                    m = by_name.get(c, {})
                    if "auto_increment" in m.get("extra", "") or "generated" in m.get("extra", ""):
                        continue
                    newv = getters[c]()
                    if newv is None:
                        continue
                    names.append(c)
                    vals.append(None if (newv == "" and m.get("nullable")) else newv)
                if not names:
                    self.status.set_text("Nothing to insert"); return
                sql = (f"INSERT INTO {q_ident(db)}.{q_ident(table)} "
                       f"({', '.join(q_ident(c) for c in names)}) "
                       f"VALUES ({', '.join(q_val(v) for v in vals)})")
                if not self.confirm(f"Insert a new row into {db}.{table}?\n\n{sql}"):
                    return
                self._exec_form_sql(sql, endpoint, db, win, ctx,
                                    f"Inserted a row into {db}.{table}")
                return
            changed = {}
            for c in cols:
                m = by_name.get(c, {})
                if "auto_increment" in m.get("extra", "") or "generated" in m.get("extra", ""):
                    continue
                newv = getters[c]()
                if newv is None:                  # read-only widget
                    continue
                if newv == "" and m.get("nullable"):   # empty nullable field -> NULL
                    if orig[c] != NULL:
                        changed[c] = None
                elif newv != orig[c]:
                    changed[c] = newv
            if not changed:
                self.status.set_text("No changes to save"); return
            if not ident:
                self.status.set_text("Can't save: no way to identify this row"); return
            where = self._where(ident, orig)
            sets = ", ".join(f"{q_ident(c)}={q_val(v)}" for c, v in changed.items())
            sql = f"UPDATE {q_ident(db)}.{q_ident(table)} SET {sets} WHERE {where} LIMIT 1"
            if not self.confirm(f"Save {len(changed)} change(s) to this row?\n\n{sql}"):
                return

            def applied():
                lims = ctx.get("limits") or [200] * len(cols)
                for c, v in changed.items():
                    nv = NULL if v is None else v
                    ci = cols.index(c)
                    orig[c] = nv
                    values[ci] = nv
                    store[idx][ci] = _cell_display(nv, lims[ci])
                self.status.set_text(f"Saved {len(changed)} change(s) to {db}.{table}")

            def work():
                try:
                    endpoint.query(sql, db=db)
                    GLib.idle_add(applied)
                except Exception as e:
                    GLib.idle_add(self.status.set_text, str(e))
            self._bg(work)

        def on_delete(_b):
            if not ident:
                return
            where = self._where(ident, orig)
            sql = f"DELETE FROM {q_ident(db)}.{q_ident(table)} WHERE {where} LIMIT 1"
            if not self.confirm(f"Delete this row from {db}.{table}?\n\n{sql}"
                                "\n\nThis cannot be undone."):
                return
            self._exec_form_sql(sql, endpoint, db, win, ctx,
                                f"Deleted 1 row from {db}.{table}")

        revert.connect("clicked", on_revert)
        save.connect("clicked", on_save)
        back.connect("clicked", lambda _b: win.destroy())
        if not is_new:
            delete.connect("clicked", on_delete)

        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vb.pack_start(gsw, True, True, 0)
        vb.pack_start(hint, False, False, 0)
        vb.pack_start(bar, False, False, 0)
        win.add(vb); win.show_all()

    def _exec_form_sql(self, sql, endpoint, db, win, ctx, ok_msg):
        """Run an insert/delete from a row form, then close it and reload the
        grid so the totals and rows reflect the change."""
        def work():
            try:
                endpoint.query(sql, db=db)
                GLib.idle_add(done)
            except Exception as e:
                GLib.idle_add(self.status.set_text, str(e))

        def done():
            self.status.set_text(ok_msg)
            win.destroy()
            self._reload_table(ctx)
        self._bg(work)

    def copy_active(self, move=False):
        self._copy_between(self._active, move)

    def _copy_between(self, src, move=False):
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
