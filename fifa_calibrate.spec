# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: FIFA Calibrate (ZWO ASI camera + Standa/XIMC stage).

Build:  .venv\\Scripts\\pyinstaller --noconfirm --clean fifa_calibrate.spec
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP_NAME = "FIFA Calibrate"
CONSOLE = bool(os.environ.get("FIFA_BUILD_CONSOLE"))
PROJECT = os.path.abspath(SPECPATH)

datas = []

# ------------------------------------------------------------- libximc DLLs
# libximc ships its native libraries as package DATA, not as extension modules,
# so PyInstaller's binary analyser never sees them. Its loader
# (libximc/lowlevel/_lowlevel.py::_load_lib) resolves them relative to its own
# __file__:
#     cwd = dirname(__file__)                    -> <bundle>/libximc/lowlevel
#     WinDLL(join(cwd, r"..\library-files\win64\bindy.dll"), winmode=RTLD_GLOBAL)
#     ... then xiwrapper.dll, then libximc.dll
# so the files must land at <bundle>/libximc/library-files/win64/, which is
# exactly the layout collect_data_files preserves. Because each dependency is
# preloaded by absolute path with RTLD_GLOBAL, no os.add_dll_directory call is
# needed. Ship win64 only; libjximc.dll is the Java bridge we never use.
datas += collect_data_files(
    "libximc",
    includes=[
        "library-files/win64/bindy.dll",
        "library-files/win64/xiwrapper.dll",
        "library-files/win64/libximc.dll",
    ],
)

# ----------------------------------------------------------------- ASI DLL
# Shipped as data, not as a binary: we want a byte-for-byte copy with no
# dependency analysis. It lands at <bundle>/vendor/zwo/ASICamera2.dll as the
# last-resort fallback; build.ps1 also copies it next to the .exe, which is the
# path a user can overwrite to drop in a newer SDK.
_ASI = os.path.join(PROJECT, "vendor", "zwo", "ASICamera2.dll")
if not os.path.exists(_ASI):
    raise SystemExit(
        f"ASICamera2.dll not found at {_ASI}.\n"
        "It is ZWO's proprietary SDK binary and is not kept in this repository.\n"
        r"Run:  .\tools\fetch_asi_dll.ps1"
    )
datas += [(_ASI, "vendor/zwo")]

hiddenimports = [
    "libximc", "libximc.highlevel", "libximc.lowlevel", "libximc.lowlevel._lowlevel",
    "zwoasi",
    "PySide6.QtSvg",
]
hiddenimports += collect_submodules("fifa_gui")

excludes = [
    # Calibrate needs none of the analysis stack: it only saves raw frames.
    "scipy", "pandas", "matplotlib", "photutils", "astropy", "astropy_iers_data",
    "PIL", "pyerfa",
    "tkinter", "_tkinter", "Tkinter", "tcl", "tk",
    "PyQt5", "PyQt6", "PySide2", "shiboken2", "qtpy",
    "IPython", "ipykernel", "jupyter_client", "jupyter_core", "zmq", "pyzmq",
    "tornado", "debugpy", "pytest", "setuptools", "pip", "wheel",
    "astroquery", "pyvo", "requests", "urllib3", "bs4", "html5lib", "keyring",
    "ximea", "ximea_py",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtDBus", "PySide6.QtUiTools",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSensors", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtSpatialAudio",
]

a = Analysis(
    ["fifa_gui/apps/calibrate_main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

a.datas = [
    d for d in a.datas
    if "PySide6\\translations" not in d[0] and "PySide6/translations" not in d[0]
    and not d[0].startswith(("PySide6\\qml", "PySide6/qml"))
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    disable_windowed_traceback=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
