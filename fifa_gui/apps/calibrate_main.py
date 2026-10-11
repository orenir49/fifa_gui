"""Entry point for the FIFA Calibrate application."""

import os
import sys

# Before anything imports PySide6: another Qt application's stale plugin paths
# in the environment will crash ours.
for _var in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
             "QT_QPA_PLATFORM", "QT_SCALE_FACTOR"):
    os.environ.pop(_var, None)

import logging

from PySide6.QtWidgets import QApplication


def main():
    import argparse

    parser = argparse.ArgumentParser(description="FIFA Calibrate")
    parser.add_argument(
        "--no-connect", action="store_true",
        help="open without opening the camera or stage, so the app can be "
             "started while someone else is using the bench",
    )
    args = parser.parse_args()

    from fifa_gui.paths import user_data_dir

    log_path = user_data_dir() / "calibrate.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")],
    )

    from fifa_gui import __version__
    from fifa_gui.paths import app_dir, bundle_dir, is_frozen

    logging.getLogger(__name__).info(
        "FIFA Calibrate %s starting (frozen=%s, app_dir=%s, bundle_dir=%s, "
        "auto_connect=%s)",
        __version__, is_frozen(), app_dir(), bundle_dir(), not args.no_connect,
    )

    # Nothing here imports zwoasi or libximc: importing libximc loads native
    # DLLs and raises without the MSVC++ 2013 redistributable, which would stop
    # the window ever appearing. Both are imported inside the connect path.
    from fifa_gui.apps.calibrate.window import CalibrateWindow

    app = QApplication(sys.argv)
    app.setApplicationName("FIFA Calibrate")
    app.setOrganizationName("WeizmannFIFA")

    window = CalibrateWindow(auto_connect=not args.no_connect)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
