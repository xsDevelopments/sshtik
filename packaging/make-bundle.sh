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

flatpak run org.flatpak.Builder --force-clean --repo=repo build-dir packaging/com.sshtik.sshtik.yml
flatpak build-bundle repo "$OUT" com.sshtik.sshtik \
    --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo

echo
echo "Bundle written: $OUT  ($(du -h "$OUT" | cut -f1))"
echo "Install with:   flatpak install --user $OUT"
