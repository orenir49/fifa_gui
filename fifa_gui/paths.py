"""Path resolution that works from source and from a PyInstaller bundle."""

import os
import sys
from pathlib import Path


def is_frozen():
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def bundle_dir():
    """Where bundled read-only resources live.

    onedir -> <install>\\_internal ; source -> the project root.
    """
    if is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[1]


def app_dir():
    """Where the executable lives -- the folder a user can drop files into."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_path(*parts):
    return bundle_dir().joinpath(*parts)


def user_data_dir():
    """Per-user writable directory for logs."""
    base = Path(os.environ.get("APPDATA") or Path.home())
    d = base / "FIFA"
    d.mkdir(parents=True, exist_ok=True)
    return d


def unit_table_path():
    """The running per-unit results table, kept in the repo (or next to the exe)."""
    return app_dir() / "results" / "units.json"
