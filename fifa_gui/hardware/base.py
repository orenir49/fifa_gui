"""Device interfaces, plus null implementations for "nothing is connected".

The null devices are what the app holds before a probe succeeds, so the worker
never needs a `None` check and the UI never sees an AttributeError.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class DeviceError(RuntimeError):
    """Anything that went wrong talking to a device."""


class NotConnectedError(DeviceError):
    """A device was asked to act while disconnected."""


class NotAvailable(DeviceError):
    """A driver, DLL or Python package is missing on this machine.

    Carries a `remedy` the UI can show verbatim -- these failures are almost
    always fixable by the operator, if they are told what to install.
    """

    def __init__(self, message, remedy=""):
        super().__init__(message)
        self.remedy = remedy


class Aborted(DeviceError):
    """The operator stopped a long operation."""


@dataclass(frozen=True)
class DeviceInfo:
    kind: str
    name: str
    uri: str = ""
    detail: str = ""


@dataclass(frozen=True)
class StageStatus:
    position: int
    moving: bool
    flags: int = 0


class StageBase(ABC):
    @abstractmethod
    def open(self): ...
    @abstractmethod
    def close(self): ...
    @property
    @abstractmethod
    def connected(self): ...
    @abstractmethod
    def limits(self): ...
    @abstractmethod
    def position(self): ...
    @abstractmethod
    def status(self): ...
    @abstractmethod
    def move_to(self, position):
        """Start a move and return immediately -- callers poll status()."""
    @abstractmethod
    def stop(self): ...
    @abstractmethod
    def home_zero(self): ...

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()


class CameraBase(ABC):
    @abstractmethod
    def open(self): ...
    @abstractmethod
    def close(self): ...
    @property
    @abstractmethod
    def connected(self): ...
    @abstractmethod
    def info(self): ...
    @abstractmethod
    def configure(self, settings): ...
    @abstractmethod
    def capture(self, timeout_s=120.0, abort=None):
        """Expose once and return the frame as a 2-D ndarray."""
    @abstractmethod
    def temperature(self):
        """(sensor_c, cooler_percent), or (None, None) if unsupported."""

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()


class NullStage(StageBase):
    reason = "No stage connected."

    def open(self):
        raise NotConnectedError(self.reason)

    def close(self):
        pass

    @property
    def connected(self):
        return False

    def limits(self):
        raise NotConnectedError(self.reason)

    def position(self):
        raise NotConnectedError(self.reason)

    def status(self):
        raise NotConnectedError(self.reason)

    def move_to(self, position):
        raise NotConnectedError(self.reason)

    def stop(self):
        pass

    def home_zero(self):
        raise NotConnectedError(self.reason)


class NullCamera(CameraBase):
    reason = "No camera connected."

    def open(self):
        raise NotConnectedError(self.reason)

    def close(self):
        pass

    @property
    def connected(self):
        return False

    def info(self):
        raise NotConnectedError(self.reason)

    def configure(self, settings):
        raise NotConnectedError(self.reason)

    def capture(self, timeout_s=120.0, abort=None):
        raise NotConnectedError(self.reason)

    def temperature(self):
        return None, None
