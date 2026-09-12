#!/bin/sh
# Install sshTIK for the current user: app via pipx (using the distro's GTK/VTE/paramiko),
# plus the menu entry and icon. Run from a checkout: ./install.sh
set -e
cd "$(dirname "$0")"

missing() { echo "Missing $1. Install the system packages first:"; echo; cat <<'M'
  # Ubuntu / Debian
  sudo apt install gir1.2-vte-2.91 python3-paramiko python3-gi pipx
  # Fedora
  sudo dnf install vte291 python3-paramiko python3-gobject pipx
M
exit 1; }

command -v pipx >/dev/null || missing pipx
python3 -c 'import gi; gi.require_version("Vte","2.91"); from gi.repository import Vte' 2>/dev/null || missing "VTE GObject bindings"
python3 -c 'import paramiko' 2>/dev/null || missing paramiko

pipx install --force --system-site-packages .
install -Dm644 data/com.sshtik.sshtik.desktop "${XDG_DATA_HOME:-$HOME/.local/share}/applications/com.sshtik.sshtik.desktop"
install -Dm644 data/com.sshtik.sshtik.svg "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/com.sshtik.sshtik.svg"
command -v update-desktop-database >/dev/null && update-desktop-database "${XDG_DATA_HOME:-$HOME/.local/share}/applications" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true

echo
echo "Installed. Run 'sshtik' or find sshTIK in your applications menu."
pipx ensurepath >/dev/null 2>&1 || true
