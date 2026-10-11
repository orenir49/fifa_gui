"""Finding the attached devices.

Always called from the worker thread: enumeration can take seconds, and doing
it during startup on the GUI thread would make the first launch look frozen.
"""

import logging
from dataclasses import dataclass

from fifa_gui.hardware.base import DeviceError, NotAvailable, NullCamera, NullStage

logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    camera: object
    stage: object
    problems: list        # (device, message, remedy) for anything that failed

    @property
    def all_present(self):
        return self.camera.connected and self.stage.connected


def probe_camera(dll_path=None, index=0):
    from fifa_gui.hardware.zwo import ZwoCamera

    cam = ZwoCamera(index=index, dll_path=dll_path)
    cam.open()
    return cam


def probe_stage(uri=None):
    from fifa_gui.hardware.ximc import XimcStage

    stage = XimcStage(uri=uri)
    stage.open()
    return stage


def probe(dll_path=None, stage_uri=None):
    """Open whatever is present; report, rather than raise, for what is not."""
    problems = []

    try:
        camera = probe_camera(dll_path)
        logger.info("camera: %s", camera.info().name)
    except (NotAvailable, DeviceError, Exception) as exc:
        camera = NullCamera()
        problems.append(("camera", str(exc), getattr(exc, "remedy", "")))
        logger.warning("camera unavailable: %s", exc)

    try:
        stage = probe_stage(stage_uri)
        logger.info("stage: %s", stage.info().uri)
    except (NotAvailable, DeviceError, Exception) as exc:
        stage = NullStage()
        problems.append(("stage", str(exc), getattr(exc, "remedy", "")))
        logger.warning("stage unavailable: %s", exc)

    return ProbeResult(camera=camera, stage=stage, problems=problems)
