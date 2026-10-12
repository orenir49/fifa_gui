"""ZWO ASI camera (ASI294MM Pro)."""

import logging
import time

import numpy as np

from fifa_gui.hardware.asi_loader import init_asi
from fifa_gui.hardware.base import (
    Aborted, CameraBase, DeviceError, DeviceInfo, NotAvailable, NotConnectedError,
)

logger = logging.getLogger(__name__)


def list_cameras(dll_path=None):
    """Names of the attached cameras. Raises NotAvailable if the SDK is missing."""
    asi = init_asi(dll_path)
    if asi.get_num_cameras() == 0:
        return []
    return list(asi.list_cameras())


class ZwoCamera(CameraBase):
    def __init__(self, index=0, dll_path=None):
        self.index = index
        self.dll_path = dll_path
        self._asi = None
        self._cam = None
        self._props = None

    # -------------------------------------------------------------- lifecycle

    def open(self):
        self._asi = init_asi(self.dll_path)
        if self._asi.get_num_cameras() == 0:
            raise NotAvailable(
                "Unable to connect to the camera: none detected.",
                remedy=(
                    "Make sure ASIStudio, ASICap, SharpCap or a Python/Jupyter "
                    "kernel is not already using the camera, then press "
                    "Reconnect.\n\n"
                    "Otherwise check the USB3 connection and that the ZWO ASI "
                    "camera driver is installed (Device Manager should show an "
                    "ASI Camera, not an unknown device)."
                ),
            )
        self._cam = self._asi.Camera(self.index)
        self._props = self._cam.get_camera_property()
        logger.info("camera opened: %s", self._props.get("Name"))
        return self.info()

    def close(self):
        if self._cam is not None:
            try:
                self._cam.close()        # the notebook never did this, which is
            except Exception:            # why the camera stayed claimed until
                logger.exception("camera close failed")   # the kernel died
            self._cam = None
            self._props = None

    @property
    def connected(self):
        return self._cam is not None

    def info(self):
        if not self.connected:
            raise NotConnectedError("No camera connected.")
        p = self._props
        return DeviceInfo(
            kind="camera",
            name=str(p.get("Name", "ZWO camera")),
            uri=f"index {self.index}",
            detail=(f"{p.get('MaxWidth')}x{p.get('MaxHeight')}, "
                    f"{p.get('BitDepth')}-bit, {p.get('PixelSize')} um pixels"),
        )

    @property
    def pixel_size_um(self):
        return float(self._props.get("PixelSize", 2.315)) if self._props else None

    @property
    def bit_depth(self):
        return int(self._props.get("BitDepth", 16)) if self._props else 16

    # --------------------------------------------------------------- settings

    def configure(self, settings):
        """Apply CameraSettings, then read back what the camera actually took."""
        if not self.connected:
            raise NotConnectedError("No camera connected.")
        asi = self._asi

        image_type = getattr(asi, f"ASI_IMG_{settings.image_type}", asi.ASI_IMG_RAW16)
        self._cam.set_image_type(image_type)
        self._cam.set_control_value(asi.ASI_GAIN, int(settings.gain))
        # ASI_EXPOSURE is in MICROseconds. The notebook never set it at all, so
        # frames ran at whatever the SDK default happened to be -- which is how
        # the archived mast00 and mast03 datasets ended up saturated.
        self._cam.set_control_value(asi.ASI_EXPOSURE, int(settings.exposure_us))

        if self._props.get("IsCoolerCam"):
            self._cam.set_control_value(asi.ASI_COOLER_ON, 1 if settings.cooler_on else 0)
            if settings.cooler_on:
                self._cam.set_control_value(
                    asi.ASI_TARGET_TEMP, int(settings.target_temp_c)
                )

        return {
            "gain": self._cam.get_control_value(asi.ASI_GAIN)[0],
            "exposure_us": self._cam.get_control_value(asi.ASI_EXPOSURE)[0],
            "image_type": settings.image_type,
        }

    def exposure_range_us(self):
        """(min, max, default) for ASI_EXPOSURE, in microseconds.

        Taken from the camera rather than hardcoded: the ASI294MM Pro's floor is
        around 32 us, and that short end is the useful one for a bright fiber
        spot. Returns None when the camera cannot be asked.
        """
        if not self.connected:
            return None
        try:
            controls = self._cam.get_controls()
            control = controls.get("Exposure")
            if control is None:
                control = next(
                    (c for c in controls.values()
                     if c.get("ControlType") == self._asi.ASI_EXPOSURE),
                    None,
                )
            if control is not None:
                return (int(control["MinValue"]), int(control["MaxValue"]),
                        int(control["DefaultValue"]))
        except Exception:
            logger.exception("could not read the exposure range")
        return None

    def temperature(self):
        if not self.connected or not self._props.get("IsCoolerCam"):
            return None, None
        asi = self._asi
        try:
            temp = self._cam.get_control_value(asi.ASI_TEMPERATURE)[0] / 10.0
            power = self._cam.get_control_value(asi.ASI_COOLER_POWER_PERC)[0]
            return float(temp), float(power)
        except Exception:
            return None, None

    # ---------------------------------------------------------------- capture

    def capture(self, timeout_s=120.0, abort=None):
        """Expose once and return the frame.

        Split out of zwoasi's blocking Camera.capture() so the poll loop can
        honour an abort and so a stuck exposure times out instead of hanging
        the worker for ever. The notebook called start_exposure() and then
        capture() -- which starts another one -- so every frame was exposed
        twice; here it is started exactly once.
        """
        if not self.connected:
            raise NotConnectedError("No camera connected.")
        asi = self._asi

        self._cam.start_exposure()
        t0 = time.monotonic()
        while self._cam.get_exposure_status() == asi.ASI_EXP_WORKING:
            if abort is not None and abort.is_set():
                try:
                    self._cam.stop_exposure()
                except Exception:
                    pass
                raise Aborted("exposure aborted")
            if time.monotonic() - t0 > timeout_s:
                try:
                    self._cam.stop_exposure()
                except Exception:
                    pass
                raise DeviceError(f"exposure did not finish within {timeout_s:.0f} s")
            time.sleep(0.02)

        status = self._cam.get_exposure_status()
        if status != asi.ASI_EXP_SUCCESS:
            raise DeviceError(f"exposure failed (status {status})")

        data = self._cam.get_data_after_exposure(None)
        width, height, _, img_type = self._cam.get_roi_format()
        dtype = np.uint16 if img_type == asi.ASI_IMG_RAW16 else np.uint8
        # A fresh buffer per exposure, so handing it to another thread is safe.
        return np.frombuffer(data, dtype=dtype).reshape(height, width).copy()
