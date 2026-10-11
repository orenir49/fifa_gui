"""Entry point for the FIFA Calculate application."""

import os
import sys

# Before anything imports PySide6 or matplotlib. Another Qt application's stale
# plugin paths in the environment will crash ours, and MPLBACKEND stops
# matplotlib reaching for Tk.
os.environ.setdefault("MPLBACKEND", "QtAgg")
for _var in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
             "QT_QPA_PLATFORM", "QT_SCALE_FACTOR"):
    os.environ.pop(_var, None)

import logging

from PySide6.QtWidgets import QApplication


def main():
    from fifa_gui import __version__
    from fifa_gui.paths import app_dir, bundle_dir, is_frozen, user_data_dir

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(user_data_dir() / "calculate.log", encoding="utf-8"),
        ],
    )
    logging.getLogger(__name__).info(
        "FIFA Calculate %s starting (frozen=%s, app_dir=%s, bundle_dir=%s)",
        __version__, is_frozen(), app_dir(), bundle_dir(),
    )

    # An air-gapped lab PC would otherwise hang reaching for IERS tables.
    try:
        from astropy.utils.iers import conf as iers_conf

        iers_conf.auto_download = False
    except Exception:
        pass

    from fifa_gui.apps.calculate.window import CalculateWindow

    app = QApplication(sys.argv)
    app.setApplicationName("FIFA Calculate")
    app.setOrganizationName("WeizmannFIFA")

    window = CalculateWindow()
    window.show()

    # Directories named on the command line are loaded straight away.
    for arg in sys.argv[1:]:
        if os.path.isdir(arg):
            window.load_directory(arg)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
