# Packaging

## Flatpak (primary)

Local build & run (uses the checkout as source):

    flatpak install flathub org.flatpak.Builder org.gnome.Platform//50 org.gnome.Sdk//50
    flatpak run org.flatpak.Builder --user --install --force-clean build-dir packaging/com.sshtik.sshtik.yml
    flatpak run com.sshtik.sshtik

The GNOME runtime only ships VTE for GTK 4, so the manifest builds GTK 3 VTE
(plus its simdutf / fmt / fast_float build deps). `python-deps.json` pins
paramiko and its dependency chain to PyPI wheels/sdists; regenerate when
bumping versions with `flatpak-pip-generator paramiko` (flatpak-builder-tools).

### Submitting to Flathub

1. Tag a release; put the tag and its commit hash into `flathub/com.sshtik.sshtik.yml`.
2. Fork https://github.com/flathub/flathub, branch off `new-pr` (not master).
3. Copy `flathub/com.sshtik.sshtik.yml` and `flathub/python-deps.json` into the fork root.
4. Open a PR against `flathub/flathub:new-pr`. The bot builds it; a reviewer
   checks permissions (`--filesystem=home` is justified by the local file pane)
   and metadata. Reviews usually take a few days.
5. Once merged, Flathub creates `flathub/com.sshtik.sshtik` and grants you push
   access; future releases are PRs against that repo bumping the tag/commit.

## Debian / Ubuntu
    sudo apt install python3-build python3-stdeb dh-python
    python3 -m build --sdist && cd dist && py2dsc-deb sshtik-*.tar.gz
Runtime deps: python3-gi, gir1.2-gtk-3.0, gir1.2-vte-2.91, python3-paramiko

## Fedora
Runtime deps: python3-gobject, gtk3, vte291, python3-paramiko. A minimal spec:
BuildRequires python3-devel, %pyproject_wheel / %pyproject_install, plus the
three `install -Dm644` lines from the Flatpak manifest.

## Local install (any distro)
    ./install.sh            # pipx install --system-site-packages + desktop file + icon
