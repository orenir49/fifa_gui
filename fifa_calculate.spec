# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: FIFA Calculate (offline analysis, no hardware).

Build:  .venv\\Scripts\\pyinstaller --noconfirm --clean fifa_calculate.spec
Set FIFA_BUILD_CONSOLE=1 to get a console window for diagnosing a frozen crash.
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP_NAME = "FIFA Calculate"
CONSOLE = bool(os.environ.get("FIFA_BUILD_CONSOLE"))

# --------------------------------------------------------------------- datas
datas = []
# astropy's own hook ships with pyinstaller-hooks-contrib; the IERS tables are
# listed explicitly so the app stays fully offline.
datas += collect_data_files("astropy_iers_data")

# -------------------------------------------------------------- hiddenimports
hiddenimports = [
    "matplotlib.backends.backend_qtagg",
    "matplotlib.backends.backend_agg",
    "PySide6.QtPrintSupport",     # matplotlib's Qt save dialog
    "PySide6.QtSvg",
    "astropy.io.fits",
]
# Our own package, wholesale: widget modules reached only through Qt would
# otherwise be missed by the analyser.
hiddenimports += collect_submodules("fifa_gui")

# ------------------------------------------------------------------ excludes
excludes = [
    # Tk: matplotlib will pull in TkAgg otherwise (+25 MB, and a crash on a
    # machine without the tcl/tk DLLs). The entry script also sets MPLBACKEND.
    "tkinter", "_tkinter", "Tkinter", "tcl", "tk", "tcl8", "FixTk",
    "matplotlib.backends.backend_tkagg", "matplotlib.backends.backend_tkcairo",
    "matplotlib.backends._backend_tk",
    # A second Qt binding in one process is an instant crash.
    "PyQt5", "PyQt6", "PySide2", "shiboken2", "qtpy",
    # This app touches no hardware.
    "zwoasi", "libximc", "ximea", "ximea_py",
    # Dropped dependencies: polyfit replaces curve_fit, and core.centroid has
    # its own centre of mass. scipy and photutils are test-only here.
    "scipy", "photutils", "pandas",
    # Notebook and dev residue.
    "IPython", "ipykernel", "jupyter_client", "jupyter_core", "notebook",
    "nbconvert", "nbformat", "zmq", "pyzmq", "tornado", "debugpy",
    "pytest", "sphinx", "setuptools", "pip", "wheel", "doctest",
    "astroquery", "pyvo", "requests", "urllib3", "html5lib", "bs4", "keyring",
    # Qt modules we never use. Most are absent from PySide6-Essentials anyway;
    # listed so the spec still behaves if someone installs full PySide6.
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DAnimation", "PySide6.Qt3DLogic",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2", "PySide6.QtQuickTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtDBus", "PySide6.QtUiTools",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtSerialPort", "PySide6.QtSensors",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSpatialAudio",
]

a = Analysis(
    ["fifa_gui/apps/calculate_main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,     # not -OO: astropy reads docstrings in places
)

# Qt translations and QML are never loaded; dropping them saves ~20 MB.
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
    upx=False,          # UPX sharply increases antivirus false positives
    console=CONSOLE,
    disable_windowed_traceback=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
