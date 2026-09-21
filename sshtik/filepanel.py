"""Dual-pane SFTP helper: local (this machine) vs remote (shell's cwd).

Features: recursive transfers, drag-and-drop between panes (and from a desktop
file manager onto the remote pane), right-click Delete/Rename/New folder/chmod,
double-click a remote file to edit it locally with auto-upload on save."""
import fnmatch
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from urllib.parse import unquote, urlparse

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Gio, Pango

from .config import config, CACHE_DIR
from .ui import stripe, close_on_escape

DND_TARGET = "application/x-sshtik-files"
_TARGETS = [Gtk.TargetEntry.new(DND_TARGET, Gtk.TargetFlags.SAME_APP, 0),
            Gtk.TargetEntry.new("text/uri-list", 0, 1)]


def _human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


# ---------------------------------------------------------------------------
# Filesystem backends: identical interface for local and remote
# ---------------------------------------------------------------------------
class LocalFS:
    name = "Local"
    def listdir(self, path):
        for e in os.scandir(path):
            try:
                st = e.stat(follow_symlinks=True)
                yield e.name, st.st_size, stat.S_ISDIR(st.st_mode), st.st_mtime, st.st_mode
            except OSError:
                yield e.name, 0, False, 0, 0
    def isdir(self, p): return os.path.isdir(p)
    def mkdir(self, p): os.mkdir(p)
    def rename(self, a, b): os.rename(a, b)
    def chmod(self, p, mode): os.chmod(p, mode)
    def remove(self, p):
        if os.path.isdir(p) and not os.path.islink(p): shutil.rmtree(p)
        else: os.remove(p)
    def walk_files(self, root):
        """Yield (relpath, size) of every file under root (dirs first via mkdir list)."""
        for d, dirs, files in os.walk(root):
            rel = os.path.relpath(d, root)
            yield rel if rel != "." else "", None
            for f in files:
                yield os.path.join(rel, f) if rel != "." else f, os.path.getsize(os.path.join(d, f))


class RemoteFS:
    name = "Remote"
    def __init__(self, conn):
        self.conn = conn
    @property
    def sftp(self): return self.conn.sftp()
    def listdir(self, path):
        for a in self.sftp.listdir_attr(path):
            is_dir = stat.S_ISDIR(a.st_mode)
            if stat.S_ISLNK(a.st_mode):
                try: is_dir = stat.S_ISDIR(self.sftp.stat(os.path.join(path, a.filename)).st_mode)
                except OSError: pass
            yield a.filename, a.st_size or 0, is_dir, a.st_mtime or 0, a.st_mode or 0
    def isdir(self, p):
        try: return stat.S_ISDIR(self.sftp.stat(p).st_mode)
        except OSError: return False
    def mkdir(self, p): self.sftp.mkdir(p)
    def rename(self, a, b): self.sftp.rename(a, b)
    def chmod(self, p, mode): self.sftp.chmod(p, mode)
    def remove(self, p):
        # rm -rf over exec is far faster than walking via SFTP
        rc, _, err = self.conn.run("rm -rf -- " + _sq(p))
        if rc: raise OSError(err.strip())
    def walk_files(self, root):
        stack = [""]
        while stack:
            rel = stack.pop()
            yield rel, None
            for a in self.sftp.listdir_attr(os.path.join(root, rel) if rel else root):
                r = os.path.join(rel, a.filename) if rel else a.filename
                if stat.S_ISDIR(a.st_mode): stack.append(r)
                elif stat.S_ISREG(a.st_mode): yield r, a.st_size or 0


def _sq(s):
    return "'" + s.replace("'", "'\\''") + "'"


# Row icons: a themed mimetype icon per file category, matched on extension.
# Icon themes disagree on which names they ship (Adwaita in the Flatpak has
# x-office-database, many desktop themes only have application-sql, etc.), so
# each category is a preference chain and _resolve_icon picks the first the
# current theme actually has — falling back to a name every theme carries.
_ICON_IMAGE = {"jpg", "jpeg", "png", "gif", "svg", "tif", "tiff", "bmp",
               "psd", "webp", "ico", "heic", "avif"}
_ICON_SCRIPT = {"php", "py", "js", "ts", "jsx", "tsx", "sh", "bash", "zsh",
                "rb", "pl", "pm", "lua", "c", "cpp", "cc", "h", "hpp", "go",
                "rs", "java", "kt", "swift", "vue", "css", "scss", "html",
                "htm", "xml", "yml", "yaml", "json", "toml", "ini", "conf"}
_ICON_NOTE = {"txt", "me", "md", "markdown", "rst", "log", "text", "nfo", "rtf"}
_ICON_DB = {"sql", "sqlite", "sqlite3", "db", "dump"}

_ICON_CHAINS = {
    "image":   ("image-x-generic",),
    "db":      ("x-office-database", "application-sql", "application-x-sqlite3", "text-x-generic"),
    "script":  ("text-x-script", "application-x-executable", "text-x-generic"),
    "note":    ("x-office-document", "text-x-generic"),
    "generic": ("text-x-generic",),
}
_icon_cache = {}


def _resolve_icon(cat):
    """First icon name in the category's chain that the theme has (cached)."""
    name = _icon_cache.get(cat)
    if name is None:
        chain = _ICON_CHAINS[cat]
        try:
            it = Gtk.IconTheme.get_default()
            name = next((n for n in chain if it.has_icon(n)), chain[-1])
        except Exception:
            name = chain[0]
        _icon_cache[cat] = name
    return name


def _file_icon(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in _ICON_IMAGE:
        return _resolve_icon("image")
    if ext in _ICON_DB:
        return _resolve_icon("db")
    if ext in _ICON_SCRIPT:
        return _resolve_icon("script")
    if ext in _ICON_NOTE:
        return _resolve_icon("note")
    return _resolve_icon("generic")


_W_BOLD, _W_NORMAL = int(Pango.Weight.BOLD), int(Pango.Weight.NORMAL)


# ---------------------------------------------------------------------------
class _Pane(Gtk.Box):
    COL_NAME, COL_SIZE, COL_MTIME, COL_PERM, COL_KIND, COL_BYTES, COL_ICON, COL_WEIGHT = range(8)

    def __init__(self, panel, fs, side):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.get_style_context().add_class("sshtik-pane")
        self.panel, self.fs, self.side = panel, fs, side
        self.cwd = None
        hdr = Gtk.Box(spacing=4)
        self.path_entry = Gtk.Entry()
        self.path_entry.connect("activate", lambda e: self.load(e.get_text()))
        up = Gtk.Button.new_from_icon_name("go-up", Gtk.IconSize.BUTTON)
        up.connect("clicked", lambda b: self.load(os.path.dirname(self.cwd.rstrip("/")) or "/"))
        rf = Gtk.Button.new_from_icon_name("view-refresh", Gtk.IconSize.BUTTON)
        rf.connect("clicked", lambda b: self.refresh())
        hdr.pack_start(Gtk.Label(label=fs.name), False, False, 4)
        hdr.pack_start(self.path_entry, True, True, 0)
        hdr.pack_start(up, False, False, 0)
        hdr.pack_start(rf, False, False, 0)
        self.pack_start(hdr, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, str, str, int, str, int)
        self.view = Gtk.TreeView(model=self.store)
        # Name column: a mimetype icon, then the name (bold for directories).
        name_col = Gtk.TreeViewColumn("Name")
        icon_rend = Gtk.CellRendererPixbuf(); icon_rend.set_property("xpad", 4)
        name_col.pack_start(icon_rend, False)
        name_col.add_attribute(icon_rend, "icon-name", self.COL_ICON)
        name_rend = Gtk.CellRendererText()
        name_rend.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        name_rend.set_property("xpad", 4)
        name_col.pack_start(name_rend, True)
        name_col.add_attribute(name_rend, "text", self.COL_NAME)
        name_col.add_attribute(name_rend, "weight", self.COL_WEIGHT)
        name_col.set_resizable(True); name_col.set_min_width(260)
        name_col.set_sort_column_id(self.COL_NAME)
        self.view.append_column(name_col)
        for t, i in (("Size", self.COL_SIZE), ("Modified", self.COL_MTIME), ("Perms", self.COL_PERM)):
            rend = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(t, rend, text=i)
            col.set_resizable(True)
            col.set_sort_column_id(self.COL_BYTES if i == self.COL_SIZE else i)
            self.view.append_column(col)
        self.view.set_grid_lines(Gtk.TreeViewGridLines.VERTICAL)
        stripe(self.view)
        self.view.connect("row-activated", self._activated)
        self.view.connect("button-press-event", self._click)
        self.view.connect("key-press-event", self._key)
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        sw = Gtk.ScrolledWindow(); sw.add(self.view)
        self.pack_start(sw, True, True, 0)

        # drag-and-drop: rows out, rows/uris in
        self.view.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, _TARGETS[:1], Gdk.DragAction.COPY)
        self.view.connect("drag-data-get", self._drag_get)
        self.view.drag_dest_set(Gtk.DestDefaults.ALL, _TARGETS if side == "remote" else _TARGETS[:1], Gdk.DragAction.COPY)
        self.view.connect("drag-data-received", self._drag_received)

    # ---- listing --------------------------------------------------------
    def load(self, path):
        if self.side == "local":
            path = os.path.expanduser(path)
        self.panel.bg(self._load_bg, path)

    def _load_bg(self, path):
        entries = sorted(self.fs.listdir(path), key=lambda e: (not e[2], e[0].lower()))
        GLib.idle_add(self._fill, path, entries)

    def _fill(self, path, entries):
        self.cwd = path
        self.path_entry.set_text(path)
        self.store.clear()
        self.store.append(["..", "", "", "", "dir", -1, "folder", _W_BOLD])
        for name, size, is_dir, mtime, mode in entries:
            self.store.append([name, "" if is_dir else _human(size),
                               time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "",
                               oct(stat.S_IMODE(mode))[2:] if mode else "",
                               "dir" if is_dir else "file", -1 if is_dir else size,
                               "folder" if is_dir else _file_icon(name),
                               _W_BOLD if is_dir else _W_NORMAL])
        self.panel.status.set_text(f"{self.fs.name}: {len(entries)} items in {path}")

    def refresh(self):
        if self.cwd: self.load(self.cwd)

    def join(self, name):
        return os.path.join(self.cwd, name)

    def selected(self):
        model, rows = self.view.get_selection().get_selected_rows()
        return [(model[r][self.COL_NAME], model[r][self.COL_KIND]) for r in rows if model[r][self.COL_NAME] != ".."]

    def apply_pattern(self, text, select=True):
        """Select/deselect rows whose name matches any space/';'-separated glob
        (case-insensitive). '*' and '*.*' both mean everything (DOS idiom)."""
        patterns = [p for p in text.replace(";", " ").split() if p]
        if not patterns:
            return
        def matches(name):
            low = name.lower()
            for p in patterns:
                if p in ("*", "*.*") or fnmatch.fnmatch(low, p.lower()):
                    return True
            return False
        sel = self.view.get_selection()
        n = 0
        for row in self.store:
            if row[self.COL_NAME] == ".." or not matches(row[self.COL_NAME]):
                continue
            (sel.select_iter if select else sel.unselect_iter)(row.iter)
            n += 1
        self.panel.status.set_text(
            f"{'Selected' if select else 'Deselected'} {n} matching {' '.join(patterns)}")

    # ---- events ---------------------------------------------------------
    def _activated(self, view, tree_path, col):
        name, kind = self.store[tree_path][self.COL_NAME], self.store[tree_path][self.COL_KIND]
        if kind == "dir":
            self.load(os.path.normpath(self.join(name)))
        elif self.side == "remote":
            self.panel.edit_remote(self.join(name))
        else:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(self.join(name)).get_uri(), None)

    def _key(self, view, ev):
        if ev.keyval == Gdk.KEY_Delete:
            self.delete_selected(); return True
        if ev.keyval == Gdk.KEY_F2:
            self.rename_selected(); return True
        if ev.keyval == Gdk.KEY_BackSpace:
            self.load(os.path.dirname(self.cwd.rstrip("/")) or "/"); return True

    def _click(self, view, ev):
        if ev.button != 3:
            return False
        hit = view.get_path_at_pos(int(ev.x), int(ev.y))
        sel = view.get_selection()
        if hit and not sel.path_is_selected(hit[0]):
            sel.unselect_all(); sel.select_path(hit[0])
        items = self.selected()
        menu = Gtk.Menu()
        def add(label, cb, sensitive=True):
            mi = Gtk.MenuItem(label=label); mi.connect("activate", lambda m: cb()); mi.set_sensitive(sensitive)
            menu.append(mi)
        other = "Upload →" if self.side == "local" else "← Download"
        add(other, lambda: self.panel.transfer(self), bool(items))
        if self.side == "remote":
            add("Edit locally", lambda: self.panel.edit_remote(self.join(items[0][0])),
                len(items) == 1 and items[0][1] == "file")
        menu.append(Gtk.SeparatorMenuItem())
        add("New folder…", self.mkdir)
        add("Rename… (F2)", self.rename_selected, len(items) == 1)
        add("Permissions…", self.chmod_selected, bool(items))
        add("Delete (Del)", self.delete_selected, bool(items))
        menu.append(Gtk.SeparatorMenuItem())
        add("Copy path", lambda: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(
            "\n".join(self.join(n) for n, _ in items) or self.cwd, -1))
        add("Refresh", self.refresh)
        menu.show_all(); menu.popup_at_pointer(ev)
        return True

    # ---- file ops -------------------------------------------------------
    def mkdir(self):
        name = self.panel.prompt("New folder", "Name:")
        if name:
            self.panel.bg(lambda: (self.fs.mkdir(self.join(name)), GLib.idle_add(self.refresh)))

    def rename_selected(self):
        items = self.selected()
        if len(items) != 1: return
        old = items[0][0]
        new = self.panel.prompt("Rename", "New name:", old)
        if new and new != old:
            self.panel.bg(lambda: (self.fs.rename(self.join(old), self.join(new)), GLib.idle_add(self.refresh)))

    def chmod_selected(self):
        items = self.selected()
        if not items: return
        cur = ""
        for row in self.store:
            if row[self.COL_NAME] == items[0][0]: cur = row[self.COL_PERM]
        mode = self.panel.prompt("Permissions", "Octal mode (e.g. 644, 755):", cur)
        if not mode: return
        try: m = int(mode, 8)
        except ValueError:
            self.panel.status.set_text("Invalid mode"); return
        def work():
            for n, _ in items: self.fs.chmod(self.join(n), m)
            GLib.idle_add(self.refresh)
        self.panel.bg(work)

    def delete_selected(self):
        items = self.selected()
        if not items: return
        names = ", ".join(n for n, _ in items[:5]) + (" …" if len(items) > 5 else "")
        if not self.panel.confirm(f"Delete {len(items)} item(s) on {self.fs.name}?\n{names}"):
            return
        def work():
            for n, _ in items: self.fs.remove(self.join(n))
            GLib.idle_add(self.refresh)
        self.panel.bg(work)

    # ---- drag and drop --------------------------------------------------
    def _drag_get(self, view, ctx, data, info, t):
        payload = json.dumps({"side": self.side, "paths": [self.join(n) for n, _ in self.selected()]})
        data.set(data.get_target(), 8, payload.encode())

    def _drag_received(self, view, ctx, x, y, data, info, t):
        if info == 1:  # text/uri-list from a file manager -> upload
            paths = [unquote(urlparse(u).path) for u in data.get_uris()]
            if self.side == "remote" and paths:
                self.panel.transfer_paths(self.panel.local, self, paths)
        else:
            try: payload = json.loads(data.get_data().decode())
            except Exception: payload = None
            if payload and payload["side"] != self.side and payload["paths"]:
                src = self.panel.local if payload["side"] == "local" else self.panel.remote
                self.panel.transfer_paths(src, self, payload["paths"])
        Gtk.drag_finish(ctx, True, False, t)


# ---------------------------------------------------------------------------
class FilePanel(Gtk.Window):
    def __init__(self, conn, parent=None):
        super().__init__(title=f"Files — {conn.host}" + (f" (via {conn.via.host})" if conn.via else ""))
        self.conn = conn
        w, h = config["window"].get("files", (1100, 650))
        self.set_default_size(w, h)
        if parent:
            self.set_transient_for(parent)
        self.connect("delete-event", self._on_close)
        close_on_escape(self)
        self.connect("key-press-event", self._on_key)
        self._monitors = []      # keep edit-file monitors alive
        self._edit_timers = {}   # local path -> pending debounce timer id

        self.local = _Pane(self, LocalFS(), "local")
        self.remote = _Pane(self, RemoteFS(conn), "remote")
        self.remote.fs.name = f"Remote ({conn.host})"
        self._active = self.local
        for pane in (self.local, self.remote):
            # any focus inside a pane (its list or its path entry) makes it active
            for fw in (pane.view, pane.path_entry):
                fw.connect("focus-in-event",
                           lambda w, e, p=pane: (self._set_active(p), False)[1])
        self._set_active(self.local)

        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        mid.set_valign(Gtk.Align.CENTER)
        up = Gtk.Button(label="→"); up.set_tooltip_text("Upload to remote")
        up.connect("clicked", lambda b: self.transfer(self.local))
        dn = Gtk.Button(label="←"); dn.set_tooltip_text("Download to local")
        dn.connect("clicked", lambda b: self.transfer(self.remote))
        mid.pack_start(up, False, False, 0); mid.pack_start(dn, False, False, 0)

        self.paned = Gtk.Paned()
        self.paned.pack1(self.local, True, False)
        rbox = Gtk.Box(spacing=4)
        rbox.pack_start(mid, False, False, 0)
        rbox.pack_start(self.remote, True, True, 0)
        self.paned.pack2(rbox, True, False)
        self.paned.set_position(config["window"].get("files_paned", w // 2))

        self.progress = Gtk.ProgressBar(show_text=True)
        self.status = Gtk.Label(xalign=0); self.status.set_ellipsize(Pango.EllipsizeMode.END)
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vb.pack_start(self.paned, True, True, 0)
        vb.pack_start(self.progress, False, False, 0)
        vb.pack_start(self.status, False, False, 0)
        vb.pack_start(self._hint_bar(), False, False, 0)
        self.add(vb)

        self.local.load(config["local_dir"].get(conn.host) or os.getcwd())
        self.bg(lambda: GLib.idle_add(self.remote.load, conn.shell_cwd() or "."))

    # ---- helpers --------------------------------------------------------
    def _set_active(self, pane):
        """Mark one pane active: it gets the accent frame + accent selection,
        the other drops to a muted selection. Blue = where the focus is."""
        self._active = pane
        for p in (self.local, self.remote):
            sc = p.get_style_context()
            (sc.add_class if p is pane else sc.remove_class)("active-pane")

    def _switch_sides(self):
        target = self.remote if self._active is self.local else self.local
        target.view.grab_focus()
        self._set_active(target)

    def _focus_path(self):
        self._active.path_entry.grab_focus()
        self._active.path_entry.select_region(0, -1)

    def _on_key(self, w, ev):
        focus = self.get_focus()
        k = ev.keyval
        # function keys act on the active pane regardless of what has focus
        if k == Gdk.KEY_F2: self._active.rename_selected(); return True
        if k == Gdk.KEY_F3: self.view_active(); return True
        if k == Gdk.KEY_F4: self.edit_active(); return True
        if k == Gdk.KEY_F5: self.copy_active(); return True
        if k == Gdk.KEY_F6: self.move_active(); return True
        if k == Gdk.KEY_F7: self._active.mkdir(); return True
        if k == Gdk.KEY_F8: self._active.delete_selected(); return True
        if k in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            self._switch_sides(); return True
        # the rest are text-ish, so don't fire while editing a field
        if isinstance(focus, Gtk.Entry):
            return False
        if k == Gdk.KEY_slash:
            self._focus_path(); return True
        if k in (Gdk.KEY_plus, Gdk.KEY_KP_Add):
            self._pattern_popover(self._active, True); return True
        if k in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self._pattern_popover(self._active, False); return True
        return False

    def _hint_bar(self):
        bar = Gtk.Box(spacing=2)
        bar.get_style_context().add_class("toolbar")
        items = (("Esc", "Close", self.close),
                 ("Tab", "Switch", self._switch_sides),
                 ("/", "Location", self._focus_path),
                 ("+", "Select", lambda: self._pattern_popover(self._active, True)),
                 ("−", "Deselect", lambda: self._pattern_popover(self._active, False)),
                 ("F2", "Rename", lambda: self._active.rename_selected()),
                 ("F3", "View", self.view_active),
                 ("F4", "Edit", self.edit_active),
                 ("F5", "Copy", self.copy_active),
                 ("F6", "Move", self.move_active),
                 ("F7", "New Folder", lambda: self._active.mkdir()),
                 ("F8", "Delete", lambda: self._active.delete_selected()))
        for key, label, cb in items:
            b = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
            b.set_can_focus(False)  # keep keyboard focus on the file lists
            lbl = Gtk.Label()
            lbl.set_markup(f"<b>{key}</b> {label}")
            b.add(lbl)
            b.connect("clicked", lambda _b, cb=cb: cb())
            bar.pack_start(b, True, True, 0)
        return bar

    def copy_active(self):
        self.transfer(self._active)

    def move_active(self):
        pane = self._active
        items = [pane.join(n) for n, _ in pane.selected()]
        if not items:
            return
        dst = self.remote if pane is self.local else self.local
        if self.confirm(f"Move {len(items)} item(s) to {dst.fs.name}: {dst.cwd}?"):
            self.transfer_paths(pane, dst, items, move=True)

    def _pattern_popover(self, pane, select):
        pop = Gtk.Popover()
        pop.set_relative_to(pane.view)
        pop.set_position(Gtk.PositionType.TOP)
        box = Gtk.Box(spacing=6)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(6)
        box.add(Gtk.Label(label=("Select" if select else "Deselect") + " pattern:"))
        entry = Gtk.Entry()
        entry.set_placeholder_text("*.php   img*   *")
        entry.set_width_chars(18)
        box.add(entry)
        pop.add(box)
        def go(*_):
            pane.apply_pattern(entry.get_text(), select=select)
            pop.popdown()
        entry.connect("activate", go)
        pop.show_all()
        entry.grab_focus()

    def _on_close(self, *_):
        config["window"]["files"] = list(self.get_size())
        config["window"]["files_paned"] = self.paned.get_position()
        if self.local.cwd: config["local_dir"][self.conn.host] = self.local.cwd
        config.save()
        return False

    def bg(self, fn, *args):
        def work():
            try: fn(*args)
            except Exception as e: GLib.idle_add(self.status.set_text, f"Error: {e}")
        threading.Thread(target=work, daemon=True).start()

    def prompt(self, title, label, default=""):
        d = Gtk.Dialog(title=title, transient_for=self, flags=0)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        box = d.get_content_area(); box.set_spacing(6); box.set_margin_start(12); box.set_margin_end(12)
        box.add(Gtk.Label(label=label, xalign=0))
        e = Gtk.Entry(text=default, activates_default=True); box.add(e)
        d.set_default_response(Gtk.ResponseType.OK); d.show_all()
        v = e.get_text() if d.run() == Gtk.ResponseType.OK else None
        d.destroy(); return v

    def confirm(self, text):
        md = Gtk.MessageDialog(transient_for=self, message_type=Gtk.MessageType.QUESTION,
                               buttons=Gtk.ButtonsType.OK_CANCEL, text=text)
        ok = md.run() == Gtk.ResponseType.OK; md.destroy(); return ok

    # ---- transfers ------------------------------------------------------
    def transfer(self, src_pane):
        paths = [src_pane.join(n) for n, _ in src_pane.selected()]
        dst = self.remote if src_pane is self.local else self.local
        self.transfer_paths(src_pane, dst, paths)

    def transfer_paths(self, src, dst, paths, move=False):
        if not paths: return
        upload = src is self.local
        sftp = self.conn.sftp()

        def work():
            # 1. build the job list (dirs to create, files with sizes)
            jobs, total = [], 0
            for p in paths:
                base = os.path.basename(p.rstrip("/"))
                if src.fs.isdir(p):
                    for rel, size in src.fs.walk_files(p):
                        d = os.path.join(base, rel) if rel else base
                        jobs.append((os.path.join(p, rel) if rel else p, os.path.join(dst.cwd, d), size))
                        total += size or 0
                else:
                    size = (os.path.getsize(p) if upload else sftp.stat(p).st_size) or 0
                    jobs.append((p, os.path.join(dst.cwd, base), size)); total += size
            # 2. run them
            done_bytes, nfiles = 0, sum(1 for j in jobs if j[2] is not None)
            count = 0
            errors = []
            for s, d, size in jobs:
                if size is None:  # directory
                    try: dst.fs.mkdir(d)
                    except OSError: pass
                    continue
                count += 1
                name = os.path.basename(s)
                base_done = done_bytes
                def cb(x, _t, name=name, base_done=base_done):
                    frac = (base_done + x) / total if total else 1
                    GLib.idle_add(self._progress, frac, f"{count}/{nfiles} {name}")
                try:
                    (sftp.put if upload else sftp.get)(s, d, callback=cb)
                except Exception as e:
                    errors.append((name, e))
                done_bytes += size
            if errors:
                ok = nfiles - len(errors)
                fname, err = errors[0]
                more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
                GLib.idle_add(self._progress, 0.0, "")
                GLib.idle_add(self.status.set_text,
                              f"{fname}: {err}{more} — {ok}/{nfiles} transferred")
            else:
                verb = "Moved" if move else "Copied"
                GLib.idle_add(self._progress, 1.0, f"{verb} {nfiles} file(s), {_human(total)}")
                if move:  # delete sources only after a fully clean copy
                    for p in paths:
                        try: src.fs.remove(p)
                        except Exception: pass
                    GLib.idle_add(src.refresh)
            GLib.idle_add(dst.refresh)
        self.bg(work)

    def _progress(self, frac, text):
        self.progress.set_fraction(min(frac, 1.0)); self.progress.set_text(text)

    # ---- edit remote file locally ----------------------------------------
    # ---- view (default app) vs edit (text editor) -----------------------
    def _cache_path(self, remote_path):
        local = os.path.join(CACHE_DIR, "edit", self.conn.host, remote_path.lstrip("/"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        return local

    def _default_open(self, path):
        """Open with the desktop's default handler (browser for html, etc.)."""
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)

    def _text_editor_launch(self, path):
        """Open in a text editor: the configured one, else the system default
        for text/plain, else the generic handler."""
        editor = config.get("editor") or ""
        if editor and not os.path.exists("/.flatpak-info"):
            subprocess.Popen(editor.split() + [path]); return
        app = None
        if not os.path.exists("/.flatpak-info"):
            app = Gio.AppInfo.get_default_for_type("text/plain", False)
        if app is not None:
            app.launch([Gio.File.new_for_path(path)], None)
        else:
            self._default_open(path)  # Flatpak: portal opens the default text app

    def view_active(self):
        pane = self._active
        files = [n for n, k in pane.selected() if k == "file"]
        if not files:
            return
        if pane.side == "local":
            self._default_open(pane.join(files[0]))
        else:
            rp = pane.join(files[0]); local = self._cache_path(rp)
            self.bg(lambda: (self.conn.sftp().get(rp, local),
                             GLib.idle_add(self._default_open, local)))

    def edit_active(self):
        pane = self._active
        files = [n for n, k in pane.selected() if k == "file"]
        if not files:
            return
        if pane.side == "local":
            self._text_editor_launch(pane.join(files[0]))
        else:
            self.edit_remote(pane.join(files[0]))

    def edit_remote(self, remote_path):
        local = self._cache_path(remote_path)
        def work():
            self.conn.sftp().get(remote_path, local)
            GLib.idle_add(self._open_editor, local, remote_path)
        self.bg(work)

    def _open_editor(self, local, remote_path):
        self._text_editor_launch(local)
        mon = Gio.File.new_for_path(local).monitor_file(Gio.FileMonitorFlags.NONE, None)
        mon.connect("changed", self._edited, local, remote_path)
        self._monitors.append(mon)
        self.status.set_text(f"Editing {remote_path} — saves upload automatically")

    def _edited(self, mon, f, other, ev, local, remote_path):
        if ev not in (Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED):
            return
        # A single save fires several change events (write, truncate, rename).
        # Debounce so one save = one upload, 400ms after the last event.
        if local in self._edit_timers:
            GLib.source_remove(self._edit_timers[local])
        self._edit_timers[local] = GLib.timeout_add(
            400, self._upload_edit, local, remote_path)

    def _upload_edit(self, local, remote_path):
        self._edit_timers.pop(local, None)
        def work():
            self.conn.sftp().put(local, remote_path)
            GLib.idle_add(self.status.set_text, f"Uploaded {remote_path} at {time.strftime('%H:%M:%S')}")
            GLib.idle_add(self.remote.refresh)
        self.bg(work)
        return False  # one-shot timer
