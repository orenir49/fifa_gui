"""The acquisition plan: what the Calibrate app runs, and what makes it invalid.

Pure and Qt-free, so the validation can be unit-tested without a GUI and without
a stage.
"""

import os
import shutil
from dataclasses import dataclass, field, asdict

#: distance to retract so the obscuration clears the beam, in stage steps
DEFAULT_BACKOFF = 110000
DEFAULT_SETTLE_MS = 99
DEFAULT_REPEATS = 1
#: Unity gain. The fiber spot is bright, so gain buys noise, not signal.
DEFAULT_GAIN = 0
#: ASI_OFFSET, in ADU. Lifts the floor well clear of zero: at 30 the spot
#: sat on a floor that was still clipping, which is what made it so hard to
#: see in a test exposure. The camera's own range is read on connect.
DEFAULT_OFFSET = 1800
#: Exposure is in MICROseconds, which is the unit ASI_EXPOSURE takes. The
#: ASI294MM Pro goes down to ~32 us, and that short end is the useful one here:
#: the fiber spot is bright enough that a millisecond saturates it.
DEFAULT_EXPOSURE_US = 32

#: a full-frame RAW16 ASI294MM Pro exposure, 5644 x 8288 x 2 bytes
FRAME_BYTES = 5644 * 8288 * 2


@dataclass
class CameraSettings:
    gain: int = DEFAULT_GAIN
    offset: int = DEFAULT_OFFSET
    exposure_us: int = DEFAULT_EXPOSURE_US
    image_type: str = "RAW16"
    cooler_on: bool = False
    target_temp_c: int = -10

    @property
    def exposure_ms(self):
        return self.exposure_us / 1000.0

    @property
    def exposure_s(self):
        return self.exposure_us / 1e6


@dataclass
class SequencePlan:
    targets: list = field(default_factory=list)
    out_dir: str = ""
    backoff: int = DEFAULT_BACKOFF
    settle_ms: int = DEFAULT_SETTLE_MS
    repeats: int = DEFAULT_REPEATS
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
        cam = dict(d.pop("camera", None) or {})
        # Plans written before exposure moved to microseconds.
        if "exposure_ms" in cam:
            cam["exposure_us"] = int(round(float(cam.pop("exposure_ms")) * 1000))
        return cls(camera=CameraSettings(**cam), **d)


def nearest_existing(path):
    """The closest ancestor of `path` that exists, or None if none does.

    A drive that is not mounted has no existing ancestor, which is the case
    worth telling an operator about: everything else can simply be created.
    """
    current = os.path.abspath(path)
    while True:
        if os.path.isdir(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:          # reached the root
            return None
        current = parent


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
    else:
        # A directory that is not there yet is not a problem -- the run creates
        # it. What matters is whether it CAN be created, so the writability and
        # free-space checks run against the nearest parent that does exist.
        exists = os.path.isdir(plan.out_dir)
        anchor = plan.out_dir if exists else nearest_existing(plan.out_dir)
        if not exists:
            problems.append(
                Problem("warning", f"Output directory will be created: {plan.out_dir}")
            )

        if anchor is None:
            problems.append(
                Problem("error", f"Nothing of this path exists and it cannot be "
                                 f"created: {plan.out_dir}")
            )
        elif not os.access(anchor, os.W_OK):
            problems.append(
                Problem("error", f"Output directory is not writable: {anchor}")
            )
        else:
            need = plan.frame_count * FRAME_BYTES
            free = shutil.disk_usage(anchor).free
            if free < need:
                problems.append(
                    Problem("error", f"Need {need / 1e9:.1f} GB for {plan.frame_count} frames "
                                     f"but only {free / 1e9:.1f} GB is free.")
                )

        if exists and not plan.overwrite:
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
