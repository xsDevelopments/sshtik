# sshTIK

SSH terminal with pop-up SFTP (F5) and MySQL/MariaDB (F6) helpers that piggyback
on the live session.

## Install

**Flatpak (any distro):** download the `.flatpak` from the
[latest release](https://github.com/xsDevelopments/sshtik/releases/latest), then

    flatpak install sshtik-*.flatpak

**From source:**

    # 1. system packages (the GTK/VTE bindings can't come from pip)
    sudo apt install gir1.2-vte-2.91 python3-paramiko python3-gi pipx   # Ubuntu / Debian
    sudo dnf install vte291 python3-paramiko python3-gobject pipx       # Fedora

    # 2. the app + menu entry
    git clone https://github.com/xsDevelopments/sshtik && ./sshtik/install.sh

    sshtik [user@]host [-p PORT]      # or just `sshtik` for the host picker

`install.sh` runs `pipx install --system-site-packages .` — that flag matters; without it
pipx tries to compile PyGObject from source. You can also just run `./sshtik.py` from the checkout.
Flatpak and distro packages: see [packaging/](packaging/README.md).

How it works: one paramiko Transport carries the interactive shell (VTE),
the SFTP subsystem (file panel), and short exec channels used to peek at the
shell's cwd (`/proc/<pid>/cwd`) and foreground process (`mysql` argv).

**Nested ssh:** if you `ssh` onward from inside the session and then press F5/F6,
sshTIK notices the running `ssh` client, resolves its target with that host's own
`ssh -G`, tunnels a new connection through the current one (like `ProxyJump`) and
opens the panel on the inner host. Hops are followed recursively and cached per tab.
Authentication for the inner hop comes from *your* keys/agent, so you may be asked
for a password once if the first hop's key isn't authorised there.

## Keys

| Where | Key | Action |
|---|---|---|
| Main | F5 / F6 | Files / Database panel for the current tab |
| Main | Ctrl+Shift+T / W | New connection / close tab |
| Main | Ctrl+PgUp / PgDn | Switch tab |
| Terminal | Ctrl+Shift+C / V | Copy / paste (also right-click) |
| Terminal | Ctrl + / − / 0 | Font size |
| Files | Enter / Backspace | Open / parent dir |
| Files | Del / F2 | Delete / rename (right-click for more) |
| Files | double-click remote file | Edit locally; every save uploads |
| Database | Ctrl+Enter | Run query |

## Database panel
- Click a database → it becomes the default `USE` for the editor.
- Click a table → `SELECT * … LIMIT 200`. If the table has a primary key the
  grid is editable: edit a cell → `UPDATE … WHERE pk=… LIMIT 1`. Type `NULL`
  for SQL NULL. Right-click a row for Delete / Set NULL / Copy as INSERT.
- Structure tab: `DESCRIBE` + `SHOW CREATE TABLE`. History tab: per host.
- Hyphenated names need backticks in hand-written SQL: `` select * from `users-old` ``.

## Files panel
- Drag rows between panes (or drop files from Thunar onto the remote pane).
- Directories transfer recursively.
- Set `"editor": "code --wait"` (or `mousepad`, etc.) in
  `~/.config/sshtik/config.json` to override the default opener.

Settings, saved hosts, query history and window layout live in
`~/.config/sshtik/config.json`. Remote files being edited are cached under
`~/.cache/sshtik/edit/`.
