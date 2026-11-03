"""Where the app keeps things.

Installed (PyInstaller build):  resources are bundled next to the exe (RES_DIR),
user data (settings, logs, build.json, meowdb cookie) lives in %APPDATA%\\MapleFarmTracker
unless a file named portable.txt sits next to the exe (then everything stays there).
Running from source: both are the source folder.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "MapleFarmTracker"
APP_TITLE = "MapleStory Classic Farm Tracker"
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    EXE_DIR = Path(sys.executable).resolve().parent
    RES_DIR = Path(getattr(sys, "_MEIPASS", EXE_DIR))
    if (EXE_DIR / "portable.txt").exists():
        DATA_DIR = EXE_DIR
    elif sys.platform == "win32":
        DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    else:
        DATA_DIR = Path.home() / ".config" / APP_NAME
else:
    EXE_DIR = RES_DIR = DATA_DIR = Path(__file__).resolve().parent

DATA_DIR.mkdir(parents=True, exist_ok=True)


def resource(name: str) -> Path:
    """A bundled read-only file (maple_maps.txt, build.default.json)."""
    p = RES_DIR / name
    if not p.exists() and (EXE_DIR / name).exists():
        return EXE_DIR / name
    return p


def data(name: str) -> Path:
    """A user-writable file in the data folder."""
    return DATA_DIR / name
