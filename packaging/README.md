# Packaging

## Flatpak (primary)
See the header of `com.sshtik.sshtik.yml`. `python-deps.json` pins paramiko and its
dependency chain to PyPI wheels/sdists; regenerate when bumping versions with
`flatpak-pip-generator paramiko` (from flatpak-builder-tools) or the script in git history.

For Flathub: fork https://github.com/flathub/flathub, add the manifest with
`sources: type: git, url: https://github.com/xsDevelopments/sshtik, tag: v0.1.0`, open a PR.

## Debian / Ubuntu
    sudo apt install python3-build python3-stdeb dh-python
    python3 -m build --sdist && cd dist && py2dsc-deb sshtik-*.tar.gz
Runtime deps: python3-gi, gir1.2-gtk-3.0, gir1.2-vte-2.91, python3-paramiko

## Fedora
Runtime deps: python3-gobject, gtk3, vte291, python3-paramiko. A minimal spec:
BuildRequires python3-devel, %pyproject_wheel / %pyproject_install, plus the
three `install -Dm644` lines from the Flatpak manifest.

## Local install (any distro)
    pipx install .          # or: pip install --user .
    install -Dm644 data/com.sshtik.sshtik.desktop ~/.local/share/applications/
    install -Dm644 data/com.sshtik.sshtik.svg ~/.local/share/icons/hicolor/scalable/apps/
