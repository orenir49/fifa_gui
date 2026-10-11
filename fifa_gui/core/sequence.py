"""The acquisition plan: what the Calibrate app runs, and what makes it invalid.

Pure and Qt-free, so the validation can be unit-tested without a GUI and without
a stage.
"""

import os
import shutil
from dataclasses import dataclass, field, asdict

#: distance to retract so the obscuration clears the beam, in stage steps
DEFAULT_BACKOFF = 110000
DEFAULT_SETTLE_MS = 100
DEFAULT_GAIN = 120
DEFAULT_EXPOSURE_MS = 100

#: a full-frame RAW16 ASI294MM Pro exposure, 5644 x 8288 x 2 bytes
FRAME_BYTES = 5644 * 8288 * 2


@dataclass
class CameraSettings:
    gain: int = DEFAULT_GAIN
    exposure_ms: int = DEFAULT_EXPOSURE_MS
    image_type: str = "RAW16"
    cooler_on: bool = False
    target_temp_c: int = -10


@dataclass
class SequencePlan:
    targets: list = field(default_factory=list)
    out_dir: str = ""
    backoff: int = DEFAULT_BACKOFF
    settle_ms: int = DEFAULT_SETTLE_MS
    repeats: int = 1
    overwrite: bool = False
    #: pause at each position so the operator can recentre the spot, which is
    #: what the notebook's input() prompt did
    pause_for_operator: bool = True
    camera: CameraSettings = field(default_factory=CameraSettings)

    @property
    def frame_count(self):
        return len(self.targets) * max(self.repeats, 1)

    def filename(self, achieved_position, repeat_index):
        """Name a saved frame after the position the stage actually reached.

        The notebook did the same -- which is why the files are 284999.npy when
        the target was 285000 -- and the repeat suffix matches mast00/flex.
        """
        if self.repeats > 1:
            return f"{int(achieved_position)}_{int(repeat_index)}.npy"
        return f"{int(achieved_position)}.npy"

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        cam = d.pop("camera", None) or {}
        return cls(camera=CameraSettings(**cam), **d)


@dataclass
class Problem:
    level: str   # "error" blocks the run; "warning" does not
    text: str


def validate(plan, limits=None):
    """Everything that would make this plan fail, cheapest checks first.

    `limits` is the stage's (left, right) edge settings, or None when no stage
    is connected -- the position checks are then skipped rather than guessed.
    """
    problems = []

    if not plan.targets:
        problems.append(Problem("error", "No target positions in the sequence."))
    if plan.repeats < 1:
        problems.append(Problem("error", "Repeats must be at least 1."))

    if limits is not None:
        left, right = limits
        for t in plan.targets:
            if not (left <= t <= right):
                problems.append(
                    Problem("error", f"Target {t} is outside the stage limits "
                                     f"[{left}, {right}].")
                )
            elif t - plan.backoff < left:
                # This is the one that actually bites: the retraction, not the
                # target, is what drives into the left limit.
                problems.append(
                    Problem("error", f"Target {t} minus the {plan.backoff} back-off "
                                     f"is {t - plan.backoff}, below the left limit {left}.")
                )

    if not plan.out_dir:
        problems.append(Problem("error", "No output directory selected."))
    elif not os.path.isdir(plan.out_dir):
        problems.append(Problem("error", f"Output directory does not exist: {plan.out_dir}"))
    else:
        if not os.access(plan.out_dir, os.W_OK):
            problems.append(Problem("error", f"Output directory is not writable: {plan.out_dir}"))
        else:
            need = plan.frame_count * FRAME_BYTES
            free = shutil.disk_usage(plan.out_dir).free
            if free < need:
                problems.append(
                    Problem("error", f"Need {need / 1e9:.1f} GB for {plan.frame_count} frames "
                                     f"but only {free / 1e9:.1f} GB is free.")
                )

        if not plan.overwrite:
            # Positions are achieved, not commanded, so a collision is a guess --
            # warn rather than block.
            existing = {n for n in os.listdir(plan.out_dir) if n.endswith(".npy")}
            clash = sorted(
                n for t in plan.targets for k in range(max(plan.repeats, 1))
                if (n := plan.filename(t, k)) in existing
            )
            if clash:
                problems.append(
                    Problem("warning", f"{len(clash)} file(s) in the output directory may be "
                                       f"overwritten, e.g. {clash[0]}.")
                )

    return problems


def generate_targets(start, stop, step):
    """The positions for a regular sweep, inclusive of `stop` when it lands on it."""
    if step == 0:
        raise ValueError("step must be non-zero")
    if (stop - start) * step < 0:
        raise ValueError("step points away from stop")
    out, pos = [], int(start)
    while (pos <= stop) if step > 0 else (pos >= stop):
        out.append(pos)
        pos += int(step)
    return out
