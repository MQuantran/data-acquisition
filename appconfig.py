"""Tiny persistent preferences for data_acquisition.

One JSON file in the per-user config directory:

    Windows   %APPDATA%\\DataAcquisition\\config.json
    macOS     ~/Library/Application Support/DataAcquisition/config.json
    Linux     ~/.config/DataAcquisition/config.json

Best-effort: a read failure returns the defaults, a write failure is swallowed.
Nothing here is required for the app to run -- it just makes it behave like a
normal desktop app (remembers its window and your update-check choice).

    import appconfig
    cfg = appconfig.load()
    appconfig.save(window_geometry="1200x800+40+40")
"""
from __future__ import annotations

import json
import os
import sys

APP = "DataAcquisition"

DEFAULTS: dict = {
    "check_updates_at_startup": True,
    "window_geometry": "",
}


def config_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP)


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def load() -> dict:
    """Return the saved preferences merged onto the defaults."""
    cfg = dict(DEFAULTS)
    try:
        with open(config_path(), encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k in DEFAULTS:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def save(**changes) -> dict:
    """Update the given keys (unknown keys ignored) and write the file."""
    cfg = load()
    for k, v in changes.items():
        if k in DEFAULTS:
            cfg[k] = v
    try:
        os.makedirs(config_dir(), exist_ok=True)
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass
    return cfg


if __name__ == "__main__":
    print("config file:", config_path())
    before = load()
    print("loaded:", before)
    save(window_geometry="TEST+0+0")
    assert load()["window_geometry"] == "TEST+0+0"
    save(window_geometry=before["window_geometry"])  # restore
    print("RESULT: PASS")
