"""Version / build introspection and the About dialog."""
import os
import subprocess
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GdkPixbuf

WEBSITE = "https://sshtik.com"


def _pkg_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _repo_root():
    return os.path.dirname(_pkg_dir())


def app_version():
    try:
        import importlib.metadata as im
        return im.version("sshtik")
    except Exception:
        pass
    # not installed (run straight from a checkout): read pyproject.toml
    try:
        pp = os.path.join(_repo_root(), "pyproject.toml")
        with open(pp) as f:
            for line in f:
                if line.strip().startswith("version"):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "0.0.0"


def build_info():
    """What is actually running: version, git commit/date if run from a
    checkout, else an install date, plus the on-disk location."""
    info = {"version": app_version(), "commit": None, "date": None,
            "source": "installed", "path": _pkg_dir()}
    root = _repo_root()
    if os.path.isdir(os.path.join(root, ".git")):
        def git(*a):
            return subprocess.check_output(["git", "-C", root, *a],
                                           text=True, stderr=subprocess.DEVNULL).strip()
        try:
            info["commit"] = git("rev-parse", "--short", "HEAD")
            info["date"] = git("show", "-s", "--format=%cd", "--date=format:%Y-%m-%d %H:%M", "HEAD")
            if subprocess.call(["git", "-C", root, "diff", "--quiet"]) != 0:
                info["commit"] += "+dirty"
            info["source"] = "checkout"
        except Exception:
            pass
    if info["date"] is None:
        try:
            info["date"] = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(__file__)))
        except Exception:
            pass
    return info


def build_summary(info=None):
    """One-line ground truth, also handy for a --version flag."""
    info = info or build_info()
    v = f"sshTIK v{info['version']}"
    if info["commit"]:
        v += f" · {info['commit']}"
    if info["date"]:
        v += f" · {info['date']}"
    return v


def _logo():
    for p in (os.path.join(_repo_root(), "data", "com.sshtik.sshtik.svg"),):
        if os.path.exists(p):
            try:
                return GdkPixbuf.Pixbuf.new_from_file_at_size(p, 96, 96)
            except Exception:
                pass
    return None


def show_about(parent):
    info = build_info()
    d = Gtk.AboutDialog(transient_for=parent)
    d.set_modal(True)
    d.set_program_name("sshTIK")
    d.set_version("v" + info["version"])
    d.set_comments(
        "SSH terminal with pop-up SFTP (F5) and MySQL/MariaDB (F6) helpers\n"
        "that ride on the session you already have.\n\n"
        + ("Running from source" if info["source"] == "checkout" else "Installed build")
        + (f" · commit {info['commit']}" if info["commit"] else "")
        + (f"\nBuilt {info['date']}" if info["date"] else "")
        + f"\n{info['path']}")
    d.set_website(WEBSITE)
    d.set_website_label("sshtik.com")
    d.set_license_type(Gtk.License.GPL_3_0)
    d.set_copyright("© 2026 xsDevelopments")
    logo = _logo()
    if logo is not None:
        d.set_logo(logo)
    else:
        d.set_logo_icon_name("com.sshtik.sshtik")
    d.connect("response", lambda dlg, _r: dlg.destroy())
    d.present()
