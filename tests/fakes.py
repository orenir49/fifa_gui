"""In-process stand-ins for the camera and stage.

The real devices are on a shared bench and may be in use, so nothing in the test
suite opens them. These implement the same interfaces, move instantly, and
produce small frames.
"""

import numpy as np

from fifa_gui.hardware.base import (
    Aborted, CameraBase, DeviceInfo, NotConnectedError, StageBase, StageStatus,
)

LEFT, RIGHT = 6639, 334911      # the real bench's edge settings
SHAPE = (120, 160)


class FakeStage(StageBase):
    """Moves instantly, but reports one 'moving' status first so poll loops run."""

    def __init__(self, start=170774, fail_open=False):
        self._position = start
        self._open = False
        self._moving_ticks = 0
        self.fail_open = fail_open
        self.moves = []
        self.stopped = False
        self.closed = False

    def open(self):
        if self.fail_open:
            raise NotConnectedError("fake stage refused to open")
        self._open = True
        return self.info()

    def close(self):
        self._open = False
        self.closed = True

    @property
    def connected(self):
        return self._open

    def info(self):
        return DeviceInfo(kind="stage", name="fake stage", uri="fake://stage",
                          detail=f"limits {LEFT} .. {RIGHT}")

    def limits(self):
        return LEFT, RIGHT

    def position(self):
        return self._position

    def status(self):
        if self._moving_ticks > 0:
            self._moving_ticks -= 1
            return StageStatus(position=self._position, moving=True)
        return StageStatus(position=self._position, moving=False)

    def move_to(self, position):
        if not self._open:
            raise NotConnectedError("fake stage not open")
        self.moves.append(int(position))
        self._position = int(position)
        self._moving_ticks = 1

    def stop(self):
        self.stopped = True
        self._moving_ticks = 0

    def home_zero(self):
        self._position = 0


class FakeCamera(CameraBase):
    def __init__(self, peak=30000, fail_open=False):
        self._open = False
        self.peak = peak
        self.fail_open = fail_open
        self.settings = None
        self.exposures = 0
        self.closed = False

    def open(self):
        if self.fail_open:
            raise NotConnectedError("fake camera refused to open")
        self._open = True
        return self.info()

    def close(self):
        self._open = False
        self.closed = True

    @property
    def connected(self):
        return self._open

    def info(self):
        return DeviceInfo(kind="camera", name="fake camera", uri="fake://camera",
                          detail=f"{SHAPE[1]}x{SHAPE[0]}, 12-bit")

    def configure(self, settings):
        self.settings = settings
        return {"gain": settings.gain, "exposure_ms": settings.exposure_ms,
                "image_type": settings.image_type}

    def capture(self, timeout_s=120.0, abort=None):
        if not self._open:
            raise NotConnectedError("fake camera not open")
        if abort is not None and abort.is_set():
            raise Aborted("exposure aborted")
        self.exposures += 1
        img = np.full(SHAPE, 650, dtype=np.uint16)
        img[SHAPE[0] // 2, SHAPE[1] // 2] = self.peak
        return img

    def temperature(self):
        return -10.0, 42.0
