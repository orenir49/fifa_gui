"""Finding and initialising the ZWO ASICamera2 SDK DLL.

zwoasi 0.2.0 cannot find the DLL on its own: with no argument it only tries
ctypes.util.find_library('ASICamera2'), a PATH search, and there is no
ZWO_ASI_LIB fallback in this version. The notebook passed a CWD-relative path,
which breaks the moment the app is launched from a shortcut -- so everything
here resolves to an absolute path.
"""

import ctypes.util
import os
import struct
from pathlib import Path

from fifa_gui.hardware.base import NotAvailable
from fifa_gui.paths import app_dir, resource_path

DLL_NAME = "ASICamera2.dll"
SETTINGS_KEY = "hardware/asi_dll_path"

_initialised_with = None   # asi.init() sets a module global and raises if called twice


class AsiDllNotFound(NotAvailable):
    def __init__(self, tried):
        self.tried = list(tried)
        listing = "\n".join(f"    {p}" for p in self.tried)
        super().__init__(
            f"{DLL_NAME} was not found.",
            remedy=("Copy it next to the application, or use 'Locate DLL...' to "
                    f"point at it.\n\nSearched:\n{listing}"),
        )


def _saved_path():
    try:
        from PySide6.QtCore import QSettings

        value = QSettings("WeizmannFIFA", "FIFA").value(SETTINGS_KEY, "", type=str)
        return Path(value) if value else None
    except Exception:
        return None


def remember_path(path):
    try:
        from PySide6.QtCore import QSettings

        QSettings("WeizmannFIFA", "FIFA").setValue(SETTINGS_KEY, str(path))
    except Exception:
        pass


def candidate_paths():
    """Search order, most specific first."""
    out = []

    env = os.environ.get("FIFA_ASI_DLL")
    if env:
        out.append(Path(env))

    saved = _saved_path()
    if saved:
        out.append(saved)

    out.append(app_dir() / DLL_NAME)                    # the swappable copy
    out.append(app_dir() / "drivers" / DLL_NAME)
    out.append(resource_path("vendor", "zwo", DLL_NAME))  # the bundled fallback
    out.append(app_dir() / "vendor" / "zwo" / DLL_NAME)   # running from source

    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(var)
        if not root:
            continue
        for pattern in (f"ZWO*/**/x64/{DLL_NAME}", f"ZWO*/**/{DLL_NAME}"):
            out.extend(sorted(Path(root).glob(pattern))[:4])

    found = ctypes.util.find_library("ASICamera2")
    if found:
        out.append(Path(found))

    seen, unique = set(), []
    for p in out:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def find_asi_dll():
    tried = candidate_paths()
    for p in tried:
        if p.is_file():
            return p
    raise AsiDllNotFound(tried)


def init_asi(explicit=None):
    """Import zwoasi lazily and point it at an absolute DLL path.

    Returns the zwoasi module. Safe to call repeatedly: asi.init() raises if
    called twice, so the first successful path is remembered.
    """
    global _initialised_with

    if struct.calcsize("P") * 8 != 64:
        raise NotAvailable(
            "This is a 32-bit Python; the x64 ASICamera2.dll cannot be loaded.",
            remedy="Reinstall with a 64-bit Python 3.12.",
        )

    try:
        import zwoasi as asi
    except ImportError as exc:
        raise NotAvailable(
            f"The zwoasi package is not installed: {exc}",
            remedy="Run install.bat -Hardware.",
        ) from exc

    if _initialised_with is not None:
        return asi

    dll = Path(explicit) if explicit else find_asi_dll()
    if not dll.is_file():
        raise AsiDllNotFound([dll])

    try:
        asi.init(str(dll))
    except OSError as exc:
        if getattr(exc, "winerror", None) == 193:
            raise NotAvailable(
                f"{dll} is the 32-bit ASICamera2.dll.",
                remedy="Use the one from the SDK's lib\\x64 folder instead.",
            ) from exc
        raise NotAvailable(f"Could not load {dll}: {exc}") from exc

    _initialised_with = dll
    remember_path(dll)
    return asi
