"""Commands posted from the GUI thread onto the worker's queue."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Connect:
    dll_path: str = ""
    stage_uri: str = ""


@dataclass(frozen=True)
class Disconnect:
    pass


@dataclass(frozen=True)
class MoveTo:
    position: int


@dataclass(frozen=True)
class HomeZero:
    pass


@dataclass(frozen=True)
class ApplyCamera:
    settings: object        # core.sequence.CameraSettings


@dataclass(frozen=True)
class TestExposure:
    settings: object


@dataclass(frozen=True)
class RunSequence:
    plan: object            # core.sequence.SequencePlan


@dataclass(frozen=True)
class Shutdown:
    pass
