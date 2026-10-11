"""Loading and centroiding frames off the GUI thread."""

import logging
import traceback

import numpy as np
from PySide6.QtCore import QObject, QRunnable, Signal

from fifa_gui.apps.calculate.model import FrameRecord
from fifa_gui.core.centroid import find_centroid
from fifa_gui.core.imageio import build_pyramid, crop, load_frame

logger = logging.getLogger(__name__)

PYRAMID_FACTORS = (8, 4)
CUTOUT_SIZE = 256


class LoaderSignals(QObject):
    loaded = Signal(str, object)    # directory, FrameRecord
    failed = Signal(str, str, str)  # directory, path, message
    done = Signal(str)


class FrameLoadTask(QRunnable):
    """Load one frame, centroid it, and throw the full array away."""

    def __init__(self, directory, path, position, repeat, params, signals):
        super().__init__()
        self.directory = directory
        self.path = path
        self.position = position
        self.repeat = repeat
        self.params = params
        self.signals = signals

    def run(self):
        try:
            img = load_frame(self.path)
            res = find_centroid(
                img,
                half_width=self.params["half_width"],
                bg_offset=self.params["bg_offset"],
                clip_negative=self.params["clip_negative"],
            )
            row = res.row if res.ok else float("nan")
            col = res.col if res.ok else float("nan")
            cutout, r0, c0 = crop(
                img,
                row if np.isfinite(row) else img.shape[0] / 2,
                col if np.isfinite(col) else img.shape[1] / 2,
                CUTOUT_SIZE,
            )
            rec = FrameRecord(
                path=self.path,
                position=self.position,
                repeat=self.repeat,
                row=row,
                col=col,
                peak=res.peak,
                flags=res.flags,
                message=res.message,
                roi_bounds=res.roi_bounds,
                bkg_bounds=res.bkg_bounds,
                thumb=build_pyramid(img, PYRAMID_FACTORS),
                cutout=cutout,
                cutout_origin=(r0, c0),
                shape=img.shape,
            )
            del img      # the 93 MB array does not outlive this call
            self.signals.loaded.emit(self.directory, rec)
        except Exception as exc:
            logger.error("failed to load %s\n%s", self.path, traceback.format_exc())
            self.signals.failed.emit(self.directory, self.path, str(exc))


def read_window(path, r0, r1, c0, c1):
    """Re-read a window of a frame without loading all 93 MB of it."""
    img = load_frame(path, mmap=True)
    r0 = max(0, int(r0)); c0 = max(0, int(c0))
    r1 = min(img.shape[0], int(r1)); c1 = min(img.shape[1], int(c1))
    return np.array(img[r0:r1, c0:c1])
