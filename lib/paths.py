"""
Where the app reads and writes its files.

Run from PyCharm, the working directory is the project folder and the plain
relative paths ("sequences/", "output/", "config/") happen to work. Started
from a desktop or taskbar shortcut, the working directory is whatever Windows
decides - often C:\\Windows\\System32 - and those same paths would scatter
files across the disk or fail outright.

Everything therefore goes through DATA_ROOT:

  * frozen (PyInstaller .exe)  -> the folder holding ATI_Rig.exe
  * normal Python run          -> the project root (parent of lib/)

so a user can move the whole application folder anywhere, or run it from a
shortcut, and still find their sequences and CSVs beside the program.
"""

from __future__ import annotations

import os
import sys


def _data_root() -> str:
    if getattr(sys, "frozen", False):
        # PyInstaller: sys.executable is .../ATI_Rig/ATI_Rig.exe
        return os.path.dirname(os.path.abspath(sys.executable))
    # lib/paths.py -> lib -> project root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DATA_ROOT = _data_root()

SEQUENCES_DIR = os.path.join(DATA_ROOT, "sequences")
OUTPUT_DIR = os.path.join(DATA_ROOT, "output")
CONFIG_DIR = os.path.join(DATA_ROOT, "config")


def ensure_dirs() -> None:
    """Create the data folders if they are missing. Safe to call repeatedly."""
    for d in (SEQUENCES_DIR, OUTPUT_DIR, CONFIG_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            print(f"[WARN] Could not create {d}: {e}")


def sequence_file(name: str) -> str:
    return os.path.join(SEQUENCES_DIR, f"{name}.txt")


def output_file(name: str) -> str:
    return os.path.join(OUTPUT_DIR, name)


def config_file(name: str) -> str:
    return os.path.join(CONFIG_DIR, name)
def _resource_root() -> str:
    """Read-only files shipped with the app (icons, assets)."""
    return getattr(sys, "_MEIPASS", DATA_ROOT)


APP_ICON = (
    os.path.join(_resource_root(), "app_icon.ico")
    if getattr(sys, "frozen", False)
    else os.path.join(DATA_ROOT, "packaging", "app_icon.ico")
)