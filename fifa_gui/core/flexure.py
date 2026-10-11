"""Flexure check: scatter of repeated measurements at a single stage position."""

from dataclasses import dataclass

import numpy as np

#: acceptance circle, in microns ACROSS. The notebook drew
#: plt.Circle(centre, 30 / 2.35) -- whose second argument is the RADIUS -- so the
#: circle it plotted was 30 um in radius, 60 um across, while labelled "30 um
#: diameter". The label was the intent; enter 60 to reproduce the old plot.
DEFAULT_DIAMETER_UM = 30.0


@dataclass
class FlexureStats:
    rows: np.ndarray
    cols: np.ndarray
    mean_row: float
    mean_col: float
    deviations: np.ndarray    # radial distance of each point from the mean, px
    rms_px: float
    max_px: float
    um_per_px: float
    diameter_um: float

    @property
    def radius_px(self):
        return (self.diameter_um / 2.0) / self.um_per_px

    @property
    def rms_um(self):
        return self.rms_px * self.um_per_px

    @property
    def max_um(self):
        return self.max_px * self.um_per_px

    @property
    def passed(self):
        """True when every repeat falls inside the acceptance circle."""
        return bool(self.max_px <= self.radius_px)


def flexure_stats(rows, cols, um_per_px, diameter_um=DEFAULT_DIAMETER_UM):
    rows = np.asarray(rows, dtype=float)
    cols = np.asarray(cols, dtype=float)
    if rows.size == 0:
        raise ValueError("no frames to analyse")
    mean_row = float(rows.mean())
    mean_col = float(cols.mean())
    dev = np.hypot(rows - mean_row, cols - mean_col)
    return FlexureStats(
        rows=rows,
        cols=cols,
        mean_row=mean_row,
        mean_col=mean_col,
        deviations=dev,
        rms_px=float(np.sqrt(np.mean(dev**2))),
        max_px=float(dev.max()),
        um_per_px=float(um_per_px),
        diameter_um=float(diameter_um),
    )
