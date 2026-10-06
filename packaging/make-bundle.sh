#!/bin/sh
# Build the Flatpak and export it as a single installable .flatpak file.
# Requires: flatpak install flathub org.flatpak.Builder org.gnome.Platform//50 org.gnome.Sdk//50
# Usage: packaging/make-bundle.sh   (from the repo root)   -> dist/sshtik-<version>-<arch>.flatpak
set -e
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
