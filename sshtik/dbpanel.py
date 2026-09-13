"""MySQL/MariaDB helper. Detects a running mysql client in the shell's
foreground, reuses its -u/-h/-P/db args, and runs queries via `mysql --batch`
on exec channels. Password comes from ~/.my.cnf on the remote or is asked once.

Tabs: Data (editable grid when a table with a primary key is selected),
Structure (DESCRIBE + SHOW CREATE TABLE), History (per host, persisted)."""
import csv
import shlex
import threading

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

from .config import config
from .ui import stripe, close_on_escape, pad

NULL = "NULL"  # how `mysql --batch` prints NULL; typing it in a cell sets SQL NULL


class AuthError(RuntimeError):
    """mysql refused the credentials; the panel will ask for a password and retry."""


def _parse_mysql_argv(argv):
    """Pull user/host/port/db out of a mysql client's argv. Password is
    scrubbed by mysql itself so it never appears here."""
    opts = {"user": None, "host": None, "port": None, "db": None, "defaults": []}
    it = iter(argv[1:])
    for a in it:
        if a in ("-u", "--user"):        opts["user"] = next(it, None)
        elif a.startswith("-u"):         opts["user"] = a[2:]
        elif a.startswith("--user="):    opts["user"] = a[7:]
        elif a in ("-h", "--host"):      opts["host"] = next(it, None)
        elif a.startswith("-h"):         opts["host"] = a[2:]
        elif a.startswith("--host="):    opts["host"] = a[7:]
        elif a in ("-P", "--port"):      opts["port"] = next(it, None)
        elif a.startswith("--port="):    opts["port"] = a[7:]
        elif a.startswith("-p") or a.startswith("--password"):
            continue  # scrubbed anyway
        elif a.startswith("--defaults"): opts["defaults"].append(a)
        elif not a.startswith("-"):      opts["db"] = a
    return opts


def hdr(name):
    """Column header text: GTK treats '_' in a TreeViewColumn title as a mnemonic."""
    return name.replace("_", "__")


def q_ident(name):
    return "`" + name.replace("`", "``") + "`"


def q_val(v):
    if v is None or v == NULL:
        return "NULL"
    return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"


class DBPanel(Gtk.Window):
    def __init__(self, conn, parent=None):
        super().__init__(title=f"Database — {conn.host}" + (f" (via {conn.via.host})" if conn.via else ""))
        self.conn = conn
        w, h = config["window"].get("db", (1100, 700))
        self.set_default_size(w, h)
        if parent:
            self.set_transient_for(parent)
        self.password = None
        self.current_db = None      # set by clicking in the schema tree; default USE
        self.current_table = None   # (db, table) when grid shows a plain table select
        self.pk_cols = []           # primary key column names of current_table
        self.grid_cols = []
        self.grid_store = None
        self.connect("delete-event", self._on_close)
        close_on_escape(self)

        fg = conn.foreground_process()
        if fg and fg[1] in ("mysql", "mariadb"):
            self.opts = _parse_mysql_argv(fg[2])
        else:
            self.opts = {"user": None, "host": None, "port": None, "db": None, "defaults": []}
        if self.opts["db"]:
            self.current_db = self.opts["db"]

        self.paned = Gtk.Paned()
        self.paned.set_position(config["window"].get("db_paned", 240))

        # ---- left: schema tree ------------------------------------------
        self.tree_store = Gtk.TreeStore(str)
        self.tree = Gtk.TreeView(model=self.tree_store)
        self.tree.append_column(Gtk.TreeViewColumn("Schema", Gtk.CellRendererText(), text=0))
        self.tree.connect("test-expand-row", self._tree_expand)
        self.tree.connect("row-activated", self._tree_activated)
        self.tree.get_selection().connect("changed", self._tree_selected)
        sw = Gtk.ScrolledWindow(); sw.add(self.tree); sw.set_size_request(180, -1)
        self.paned.pack1(sw, False, False)

        # ---- right: editor + toolbar + notebook ---------------------------
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.editor = Gtk.TextView()
        self.editor.modify_font(Pango.FontDescription("monospace"))
        self.editor.set_size_request(-1, 110)
        esw = Gtk.ScrolledWindow(); esw.add(self.editor)
        right.pack_start(esw, False, False, 0)

        bar = Gtk.Box(spacing=6)
        for label, cb in (("Run (Ctrl+Enter)", lambda b: self.run_query()),
                          ("Insert row…", lambda b: self._insert_template()),
                          ("Export CSV…", lambda b: self._export_csv()),
                          ("Refresh schema", lambda b: self._load_schema())):
            btn = Gtk.Button(label=label); btn.connect("clicked", cb)
            bar.pack_start(btn, False, False, 0)
        right.pack_start(bar, False, False, 0)

        self.nb = Gtk.Notebook()
        self.grid_sw = Gtk.ScrolledWindow()
        self.nb.append_page(self.grid_sw, Gtk.Label(label="Data"))

        self.struct_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.struct_sw = Gtk.ScrolledWindow(); self.struct_sw.set_size_request(-1, 200)
        self.create_view = Gtk.TextView(editable=False)
        self.create_view.modify_font(Pango.FontDescription("monospace"))
        csw = Gtk.ScrolledWindow(); csw.add(self.create_view)
        self.struct_box.pack_start(self.struct_sw, False, False, 0)
        self.struct_box.pack_start(csw, True, True, 0)
        self.nb.append_page(self.struct_box, Gtk.Label(label="Structure"))

        self.hist_store = Gtk.ListStore(str)
        hv = Gtk.TreeView(model=self.hist_store)
        hv.append_column(Gtk.TreeViewColumn("Query", Gtk.CellRendererText(), text=0))
        hv.connect("row-activated", self._history_activated)
        hsw = Gtk.ScrolledWindow(); hsw.add(hv)
        self.nb.append_page(hsw, Gtk.Label(label="History"))
        self.nb.connect("switch-page", self._on_tab)
        right.pack_start(self.nb, True, True, 0)

        self.status = Gtk.Label(xalign=0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        right.pack_start(self.status, False, False, 0)
        self.paned.pack2(right, True, False)
        self.add(self.paned)

        self.editor.connect("key-press-event", self._key)
        self._fill_history()
        self._load_schema()

    # ---- window plumbing -----------------------------------------------
    def _on_close(self, *_):
        config["window"]["db"] = list(self.get_size())
        config["window"]["db_paned"] = self.paned.get_position()
        config.save()
        return False

    def _key(self, w, ev):
        if ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and ev.state & Gdk.ModifierType.CONTROL_MASK:
            self.run_query(); return True

    def _bg(self, fn, *args):
        """Run fn(*args) in a thread; exceptions land in the status line.
        An AuthError prompts for the MySQL password (main thread) and retries."""
        def work():
            try:
                fn(*args)
            except AuthError as e:
                GLib.idle_add(self._ask_password_then, fn, args, str(e))
            except Exception as e:
                GLib.idle_add(self.status.set_text, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _ask_password_then(self, fn, args, err):
        user = self.opts["user"] or "(default user)"
        d = Gtk.Dialog(title="MySQL password", transient_for=self, flags=0)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        box = d.get_content_area(); box.set_spacing(6)
        box.set_margin_start(12); box.set_margin_end(12); box.set_margin_top(6)
        box.add(Gtk.Label(label=f"Password for MySQL user {user} on {self.conn.host}:", xalign=0))
        if self.password is not None:
            box.add(Gtk.Label(label="(previous password was rejected)", xalign=0))
        e = Gtk.Entry(visibility=False, activates_default=True); box.add(e)
        note = Gtk.Label(label="Kept in memory for this window only; never written to disk.", xalign=0)
        note.get_style_context().add_class("dim-label"); box.add(note)
        d.set_default_response(Gtk.ResponseType.OK); d.show_all()
        ok = d.run() == Gtk.ResponseType.OK
        pw = e.get_text(); d.destroy()
        if not ok:
            self.status.set_text(err); return
        self.password = pw
        self._bg(fn, *args)

    # ---- running SQL over an exec channel ------------------------------
    def _mysql_cmd(self, sql, db=None):
        o = self.opts
        parts = ["mysql", "--batch", "--raw"] + o["defaults"]
        if o["user"]: parts += ["-u", o["user"]]
        if o["host"]: parts += ["-h", o["host"]]
        if o["port"]: parts += ["-P", o["port"]]
        use = db or self.current_db or o["db"]
        if use: parts += [use]
        cmd = " ".join(shlex.quote(p) for p in parts) + " -e " + shlex.quote(sql)
        if self.password is not None:
            # via the environment, not -p, so it never shows in `ps`
            cmd = "MYSQL_PWD=" + shlex.quote(self.password) + " " + cmd
        return cmd

    def query(self, sql, db=None):
        rc, out, err = self.conn.run(self._mysql_cmd(sql, db), timeout=120)
        if rc != 0:
            msg = err.strip() or f"mysql exited {rc}"
            if "Access denied" in msg and "using password" in msg:
                raise AuthError(msg)
            raise RuntimeError(msg)
        rows = [line.split("\t") for line in out.splitlines()]
        return (rows[0], rows[1:]) if rows else ([], [])

    def run_query(self, sql=None, table=None):
        if sql is None:
            buf = self.editor.get_buffer()
            sql = buf.get_text(*buf.get_bounds(), True).strip()
        if not sql:
            return
        self.current_table = table  # only set when we generated the SELECT ourselves
        self.status.set_text("Running…")
        config.add_history(self.conn.host, sql)
        self._fill_history()

        def work():
            cols, rows = self.query(sql)
            pk = []
            if table:
                db, t = table
                _, pkrows = self.query(
                    "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                    f"WHERE TABLE_SCHEMA={q_val(db)} AND TABLE_NAME={q_val(t)} "
                    "AND CONSTRAINT_NAME='PRIMARY' ORDER BY ORDINAL_POSITION")
                pk = [r[0] for r in pkrows]
            GLib.idle_add(self._show, cols, rows, pk)
        self._bg(work)

    # ---- data grid -----------------------------------------------------
    def _show(self, cols, rows, pk):
        for c in self.grid_sw.get_children():
            self.grid_sw.remove(c)
        self.nb.set_current_page(0)
        self.grid_cols, self.pk_cols = cols, pk
        if not cols:
            self.grid_store = None
            self.status.set_text("OK (no result set)"); return
        editable = bool(self.current_table and pk and all(c in cols for c in pk))
        self.grid_store = Gtk.ListStore(*([str] * len(cols)))
        for r in rows:
            self.grid_store.append((r + [""] * len(cols))[:len(cols)])
        tv = Gtk.TreeView(model=self.grid_store)
        tv.set_grid_lines(Gtk.TreeViewGridLines.VERTICAL)
        for i, c in enumerate(cols):
            rend = pad(Gtk.CellRendererText(editable=editable, ellipsize=Pango.EllipsizeMode.END))
            rend.set_property("width-chars", 40)
            if editable:
                rend.connect("edited", self._cell_edited, i)
            col = Gtk.TreeViewColumn(hdr(c) + (" (PK)" if c in pk else ""), rend, text=i)
            col.set_resizable(True); col.set_sort_column_id(i)
            tv.append_column(col)
        tv.connect("button-press-event", self._grid_click)
        stripe(tv)
        self.grid_sw.add(tv); tv.show_all()
        self.grid_view = tv
        note = "editable" if editable else ("read-only: no primary key" if self.current_table else "read-only")
        self.status.set_text(f"{len(rows)} rows · {note} · db: {self.current_db or '-'}")

    def _where_for_row(self, row):
        return " AND ".join(f"{q_ident(c)}={q_val(row[self.grid_cols.index(c)])}" for c in self.pk_cols)

    def _cell_edited(self, rend, path, new_text, col_idx):
        row = self.grid_store[path]
        old = row[col_idx]
        if new_text == old:
            return
        db, t = self.current_table
        sql = (f"UPDATE {q_ident(db)}.{q_ident(t)} SET {q_ident(self.grid_cols[col_idx])}={q_val(new_text)} "
               f"WHERE {self._where_for_row(list(row))} LIMIT 1")
        row[col_idx] = new_text
        def work():
            try:
                self.query(sql)
                GLib.idle_add(self.status.set_text, sql)
            except Exception as e:
                GLib.idle_add(self.status.set_text, str(e))
                GLib.idle_add(row.__setitem__, col_idx, old)
        self._bg(work)

    def _grid_click(self, tv, ev):
        if ev.button != 3:
            return False
        hit = tv.get_path_at_pos(int(ev.x), int(ev.y))
        if not hit:
            return False
        path = hit[0]
        tv.get_selection().select_path(path)
        menu = Gtk.Menu()
        if self.current_table and self.pk_cols:
            mi = Gtk.MenuItem(label="Delete row"); mi.connect("activate", lambda m: self._delete_row(path))
            menu.append(mi)
            mi = Gtk.MenuItem(label="Set cell to NULL")
            idx = tv.get_columns().index(hit[1])
            mi.connect("activate", lambda m: self._cell_edited(None, path, NULL, idx))
            menu.append(mi)
        mi = Gtk.MenuItem(label="Copy row as INSERT"); mi.connect("activate", lambda m: self._copy_insert(path))
        menu.append(mi)
        menu.show_all(); menu.popup_at_pointer(ev)
        return True

    def _delete_row(self, path):
        row = list(self.grid_store[path])
        db, t = self.current_table
        sql = f"DELETE FROM {q_ident(db)}.{q_ident(t)} WHERE {self._where_for_row(row)} LIMIT 1"
        md = Gtk.MessageDialog(transient_for=self, message_type=Gtk.MessageType.QUESTION,
                               buttons=Gtk.ButtonsType.OK_CANCEL, text=sql)
        ok = md.run() == Gtk.ResponseType.OK; md.destroy()
        if not ok:
            return
        def work():
            self.query(sql)
            GLib.idle_add(self.grid_store.remove, self.grid_store.get_iter(path))
            GLib.idle_add(self.status.set_text, sql)
        self._bg(work)

    def _copy_insert(self, path):
        row = list(self.grid_store[path])
        t = q_ident(self.current_table[1]) if self.current_table else "`table`"
        sql = f"INSERT INTO {t} ({', '.join(q_ident(c) for c in self.grid_cols)}) VALUES ({', '.join(q_val(v) for v in row)});"
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(sql, -1)
        self.status.set_text("Copied INSERT to clipboard")

    def _insert_template(self):
        if not self.grid_cols:
            return
        t = q_ident(self.current_table[1]) if self.current_table else "`table`"
        cols = ", ".join(q_ident(c) for c in self.grid_cols)
        vals = ", ".join("NULL" if c in self.pk_cols else "''" for c in self.grid_cols)
        self.editor.get_buffer().set_text(f"INSERT INTO {t} ({cols})\nVALUES ({vals});")
        self.editor.grab_focus()

    def _export_csv(self):
        if not self.grid_store:
            return
        d = Gtk.FileChooserDialog(title="Export CSV", transient_for=self, action=Gtk.FileChooserAction.SAVE)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        d.set_do_overwrite_confirmation(True)
        d.set_current_name((self.current_table[1] if self.current_table else "query") + ".csv")
        if d.run() == Gtk.ResponseType.OK:
            with open(d.get_filename(), "w", newline="") as f:
                w = csv.writer(f); w.writerow(self.grid_cols)
                for row in self.grid_store:
                    w.writerow(list(row))
            self.status.set_text(f"Exported {len(self.grid_store)} rows to {d.get_filename()}")
        d.destroy()

    # ---- structure tab -------------------------------------------------
    def _on_tab(self, nb, page, num):
        if num == 1 and self.current_table:
            self._load_structure()

    def _load_structure(self):
        db, t = self.current_table
        def work():
            cols, rows = self.query(f"DESCRIBE {q_ident(db)}.{q_ident(t)}")
            _, cr = self.query(f"SHOW CREATE TABLE {q_ident(db)}.{q_ident(t)}")
            GLib.idle_add(self._show_structure, cols, rows, cr[0][1] if cr else "")
        self._bg(work)

    def _show_structure(self, cols, rows, create):
        for c in self.struct_sw.get_children():
            self.struct_sw.remove(c)
        store = Gtk.ListStore(*([str] * len(cols)))
        for r in rows:
            store.append((r + [""] * len(cols))[:len(cols)])
        tv = Gtk.TreeView(model=store)
        for i, c in enumerate(cols):
            col = Gtk.TreeViewColumn(hdr(c), pad(Gtk.CellRendererText()), text=i); col.set_resizable(True)
            tv.append_column(col)
        stripe(tv)
        self.struct_sw.add(tv); tv.show_all()
        self.create_view.get_buffer().set_text(create.replace("\\n", "\n"))

    # ---- history tab ---------------------------------------------------
    def _fill_history(self):
        self.hist_store.clear()
        for sql in reversed(config["history"].get(self.conn.host, [])):
            self.hist_store.append([sql.replace("\n", " ")])

    def _history_activated(self, tv, path, col):
        self.editor.get_buffer().set_text(self.hist_store[path][0])
        self.nb.set_current_page(0)
        self.editor.grab_focus()

    # ---- schema tree ---------------------------------------------------
    def _load_schema(self):
        def work():
            _, dbs = self.query("SHOW DATABASES")
            GLib.idle_add(self._fill_schema, [d[0] for d in dbs])
        self._bg(work)

    def _fill_schema(self, dbs):
        self.tree_store.clear()
        for d in dbs:
            it = self.tree_store.append(None, [d])
            self.tree_store.append(it, ["(loading…)"])
            if d == self.current_db:
                self.tree.expand_row(self.tree_store.get_path(it), False)

    def _tree_expand(self, tv, it, path):
        """Lazy-load tables the first time a database row is expanded."""
        child = self.tree_store.iter_children(it)
        if child and self.tree_store[child][0] == "(loading…)":
            db = self.tree_store[it][0]
            path = path.copy()
            def work():
                _, tables = self.query("SHOW TABLES", db=db)
                GLib.idle_add(self._fill_tables, path, [t[0] for t in tables])
            self._bg(work)
        return False  # allow expansion

    def _fill_tables(self, path, tables):
        it = self.tree_store.get_iter(path)
        placeholder = self.tree_store.iter_children(it)
        for t in tables:
            self.tree_store.append(it, [t])
        if placeholder:
            self.tree_store.remove(placeholder)

    def _select_table(self, it):
        parent = self.tree_store.iter_parent(it)
        if parent is None:
            self.current_db = self.tree_store[it][0]
            self.status.set_text(f"USE {q_ident(self.current_db)}")
            return
        db, table = self.tree_store[parent][0], self.tree_store[it][0]
        if table == "(loading…)":
            return
        self.current_db = db
        sql = f"SELECT * FROM {q_ident(table)} LIMIT 200"
        self.editor.get_buffer().set_text(sql)
        self.run_query(sql, table=(db, table))

    def _tree_selected(self, sel):
        model, it = sel.get_selected()
        if it:
            self._select_table(it)

    def _tree_activated(self, tv, path, col):
        it = self.tree_store.get_iter(path)
        if self.tree_store.iter_depth(it) == 0:
            tv.expand_row(path, False) if not tv.row_expanded(path) else tv.collapse_row(path)
        else:
            self._select_table(it)
