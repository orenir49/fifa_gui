"""Verify that the FIFA GUI environment is complete. Exit code 0 means OK.

Run by install.ps1, but useful on its own when an app misbehaves on a lab PC:

    .venv\\Scripts\\python tools\\verify_env.py --hardware
"""

import argparse
import ctypes
import importlib
import os
import struct
import sys

CORE = ["numpy", "matplotlib", "PIL", "astropy", "PySide6"]
HARDWARE = ["zwoasi", "libximc.highlevel"]

VC2013_URL = "https://aka.ms/highdpimfc2013x64enu"


def check_import(name):
    try:
        mod = importlib.import_module(name)
    except Exception as exc:
        print(f"[FAIL] {name:<22} {type(exc).__name__}: {exc}")
        return False
    version = getattr(mod, "__version__", "")
    print(f"[ OK ] {name:<22} {version}")
    return True


def check_qt():
    """Importing PySide6 is not the same as having a working platform plugin."""
    try:
        from PySide6.QtCore import qVersion
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        print(f"[ OK ] {'Qt runtime':<22} {qVersion()} / platform={app.platformName()}")
        return True
    except Exception as exc:
        print(f"[FAIL] {'Qt runtime':<22} {type(exc).__name__}: {exc}")
        return False


def check_vc2013():
    """libximc calls CDLL('msvcr120.dll') at import and refuses to load without it."""
    try:
        ctypes.CDLL("msvcr120.dll")
        print(f"[ OK ] {'MSVC++ 2013 runtime':<22} (required by libximc)")
        return True
    except OSError:
        print(f"[FAIL] {'MSVC++ 2013 runtime':<22} msvcr120.dll not found")
        print(f"       The stage will not connect. Install it from {VC2013_URL}")
        return False


def check_asi_dll(root):
    dll = os.path.join(root, "vendor", "zwo", "ASICamera2.dll")
    if os.path.isfile(dll):
        print(f"[ OK ] {'ASICamera2.dll':<22} {os.path.getsize(dll) / 1e6:.1f} MB")
        return True
    print(f"[FAIL] {'ASICamera2.dll':<22} missing: {dll}")
    print("       Copy it from the ZWO ASI SDK (lib\\x64\\ASICamera2.dll).")
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hardware", action="store_true",
                    help="also check the camera and stage packages")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bits = struct.calcsize("P") * 8

    print(f"python   {sys.version.split()[0]}  ({bits}-bit)")
    print(f"exe      {sys.executable}")
    print()

    ok = True
    if bits != 64:
        print("[FAIL] 32-bit Python; the ASI and XIMC DLLs are x64 only.")
        ok = False

    for name in CORE:
        ok &= check_import(name)
    ok &= check_qt()

    if args.hardware:
        print()
        for name in HARDWARE:
            ok &= check_import(name)
        ok &= check_vc2013()
        ok &= check_asi_dll(root)
        print("       (zwoasi importing says nothing about the camera driver -- "
              "run the app to test that.)")

    print()
    print("RESULT:", "OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
