#!/bin/sh
# Build the Flatpak and export it as a single installable .flatpak file.
# Requires: flatpak install flathub org.flatpak.Builder org.gnome.Platform//50 org.gnome.Sdk//50
# Usage: packaging/make-bundle.sh   (from the repo root)   -> dist/sshtik-<version>-<arch>.flatpak
set -e

# org.flatpak.Builder reaches the host's installed runtimes over the session
# D-Bus. An SSH login on a box without a systemd user session (MX Linux) has
# none, and the build fails with "Unable to find sdk org.gnome.Sdk". Give the
# build a private session bus in that case.
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ] && [ -z "$SSHTIK_PRIVATE_DBUS" ] \
        && [ ! -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/bus" ]; then
    if command -v dbus-run-session >/dev/null 2>&1; then
        echo "No session D-Bus (SSH login?); running the build under dbus-run-session"
        SSHTIK_PRIVATE_DBUS=1 exec dbus-run-session -- "$0" "$@"
    fi
    echo "warning: no session D-Bus and no dbus-run-session (sudo apt install dbus-bin);" >&2
    echo "         the build may fail with 'Unable to find sdk'" >&2
fi

cd "$(dirname "$0")/.."
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
ARCH=$(flatpak --default-arch)
OUT="dist/sshtik-${VERSION}-${ARCH}.flatpak"
mkdir -p dist

# --disable-rofiles-fuse: cleanup via plain copies instead of a FUSE overlay.
# When org.flatpak.Builder runs as a Flatpak without working rofiles-fuse (seen
# on MX Linux), the cleanup pass corrupts the result — e.g. drops /app/bin/sshtik
# so "Finishing app" fails with "Command 'sshtik' not found". Copies are a touch
# slower but portable across hosts.
flatpak run org.flatpak.Builder --force-clean --disable-rofiles-fuse --repo=repo build-dir packaging/com.sshtik.sshtik.yml
flatpak build-bundle repo "$OUT" com.sshtik.sshtik \
    --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo

echo
echo "Bundle written: $OUT  ($(du -h "$OUT" | cut -f1))"
echo "Install with:   flatpak install --user $OUT"
