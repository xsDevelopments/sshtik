"""Persistent settings: ~/.config/sshtik/config.json"""
import json
import os

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "sshtik")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "sshtik")

_DEFAULTS = {
    "hosts": [],            # [{"name", "host", "user", "port"}]
    "font_scale": 1.0,
    "window": {},           # {"main": [w,h], "files": [w,h], "db": [w,h], "db_paned": int}
    "history": {},          # {host: [sql, ...]}  newest last
    "editor": "",           # command for editing remote files; empty = xdg-open
    "local_dir": {},        # {host: last local dir}
    "db_logins": {},        # {endpoint-key: {user,host,port,password}} — opt-in, plain text
    "db_default_host": {},  # {"host","user","port"} — left DB pane auto-connects here on open
}


class Config(dict):
    def __init__(self):
        super().__init__(_DEFAULTS)
        try:
            with open(CONFIG_FILE) as f:
                self.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self, f, indent=2)
        os.replace(tmp, CONFIG_FILE)

    def add_history(self, host, sql, limit=300):
        h = self.setdefault("history", {}).setdefault(host, [])
        if sql in h:
            h.remove(sql)
        h.append(sql)
        del h[:-limit]
        self.save()


config = Config()
