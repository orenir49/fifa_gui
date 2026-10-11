"""Standa / XIMC linear stage.

libximc is imported inside load_ximc(), never at module scope: importing it
runs `lib = ximc_shared_lib()` in libximc/lowlevel/_lowlevel.py, which loads
native DLLs and raises RuntimeError when the MSVC++ 2013 redistributable is
missing. A top-level import would stop the application opening at all on a
machine that lacks it.
"""

import logging

from fifa_gui.hardware.base import (
    DeviceError, DeviceInfo, NotAvailable, NotConnectedError, StageBase, StageStatus,
)

logger = logging.getLogger(__name__)

VC2013_URL = "https://aka.ms/highdpimfc2013x64enu"
MVCMD_RUNNING = 0x80   # MvcmdStatus.MVCMD_RUNNING


def load_ximc():
    """Import libximc.highlevel, turning its import-time failures into advice."""
    try:
        import libximc.highlevel as ximc
    except RuntimeError as exc:
        text = str(exc)
        if "msvcr120" in text or "2013" in text:
            raise NotAvailable(
                "libximc needs the Microsoft Visual C++ 2013 redistributable.",
                remedy=f"Install the x64 version from {VC2013_URL}, then reconnect.",
            ) from exc
        raise NotAvailable(
            f"libximc could not load its native libraries: {text}",
            remedy="The installation may be incomplete; re-run install.bat -Hardware.",
        ) from exc
    except ImportError as exc:
        raise NotAvailable(
            f"The libximc package is not installed: {exc}",
            remedy="Run install.bat -Hardware.",
        ) from exc
    return ximc


def enumerate_stages():
    """URIs of the attached controllers.

    ENUMERATE_PROBE only: the notebook also passed ENUMERATE_NETWORK, which
    drags in bindy network discovery for what is a local COM device.
    """
    ximc = load_ximc()
    try:
        devices = ximc.enumerate_devices(ximc.EnumerateFlags.ENUMERATE_PROBE)
    except Exception as exc:
        raise DeviceError(f"device enumeration failed: {exc}") from exc
    return [d["uri"] for d in devices]


class XimcStage(StageBase):
    def __init__(self, uri=None):
        self.uri = uri
        self._axis = None
        self._limits = None

    def open(self):
        ximc = load_ximc()
        uri = self.uri
        if not uri:
            found = enumerate_stages()
            if not found:
                # The notebook printed "a virtual device will be used" here and
                # then referenced an undefined device_uri.
                raise NotAvailable(
                    "No XIMC stage controller found.",
                    remedy=("Check the USB connection and that the XIMC driver is "
                            "installed, or type the URI manually "
                            r"(for example xi-com:\\.\COM5)."),
                )
            uri = found[0]

        axis = ximc.Axis(uri)
        try:
            axis.open_device()
        except Exception as exc:
            raise DeviceError(f"could not open {uri}: {exc}") from exc

        self._axis = axis
        self.uri = uri
        edges = axis.get_edges_settings()
        self._limits = (int(edges.LeftBorder), int(edges.RightBorder))
        logger.info("stage opened: %s, limits %s", uri, self._limits)
        return self.info()

    def close(self):
        if self._axis is not None:
            try:
                self._axis.close_device()   # never done in the notebook, which is
            except Exception:               # why a re-run sometimes could not find
                logger.exception("stage close failed")   # the COM port
            self._axis = None

    @property
    def connected(self):
        return self._axis is not None

    def info(self):
        if not self.connected:
            raise NotConnectedError("No stage connected.")
        return DeviceInfo(
            kind="stage",
            name="XIMC stage",
            uri=self.uri,
            detail=f"limits {self._limits[0]} .. {self._limits[1]}",
        )

    def limits(self):
        if not self.connected:
            raise NotConnectedError("No stage connected.")
        return self._limits

    def position(self):
        if not self.connected:
            raise NotConnectedError("No stage connected.")
        return int(self._axis.get_position().Position)

    def status(self):
        if not self.connected:
            raise NotConnectedError("No stage connected.")
        st = self._axis.get_status()
        return StageStatus(
            position=int(st.CurPosition),
            moving=bool(int(st.MvCmdSts) & MVCMD_RUNNING),
            flags=int(st.MvCmdSts),
        )

    def move_to(self, position):
        """Start a move and return at once; callers poll status().

        command_wait_for_stop() blocks inside the C library and cannot be
        interrupted or report progress, so it is deliberately not used.
        """
        if not self.connected:
            raise NotConnectedError("No stage connected.")
        self._axis.command_move(int(position), 0)

    def stop(self):
        if self.connected:
            try:
                self._axis.command_sstp()      # soft stop
            except Exception:
                logger.exception("stage stop failed")

    def home_zero(self):
        if not self.connected:
            raise NotConnectedError("No stage connected.")
        self._axis.command_homezero()
