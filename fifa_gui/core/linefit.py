"""Fitting the fiber track against stage position.

Reporting convention, inherited from the notebook and preserved here: the value
called **x is the image column** and **y is the image row**. Internally
everything is (row, col); the swap happens only at this boundary.

The fit is `np.polyfit(..., cov=True)`, which matches scipy's `curve_fit` on
this linear model to ~1e-9 relative and returns the covariance too -- so the
whole of scipy is not a dependency.
"""

from dataclasses import dataclass, field

import numpy as np


@dataclass
class TrackFit:
    """A linear fit of the spot position against stage position."""

    positions: np.ndarray    # stage positions, w
    rows: np.ndarray         # measured image rows      -> reported as y
    cols: np.ndarray         # measured image columns   -> reported as x
    coef_x: np.ndarray       # col(w) = coef_x[0] * w + coef_x[1]
    cov_x: np.ndarray
    coef_y: np.ndarray       # row(w) = coef_y[0] * w + coef_y[1]
    cov_y: np.ndarray
    residual_x: np.ndarray = field(default=None)
    residual_y: np.ndarray = field(default=None)
    residual_perp: np.ndarray = field(default=None)

    # -- evaluation ---------------------------------------------------------

    def x_at(self, w):
        """Fitted image column at stage position w."""
        return float(np.polyval(self.coef_x, w))

    def y_at(self, w):
        """Fitted image row at stage position w."""
        return float(np.polyval(self.coef_y, w))

    def _sigma(self, cov, w):
        j = np.array([w, 1.0])
        return float(np.sqrt(max(j @ cov @ j, 0.0)))

    def sigma_x_at(self, w):
        return self._sigma(self.cov_x, w)

    def sigma_y_at(self, w):
        return self._sigma(self.cov_y, w)

    def position_for_x(self, x_target):
        """Stage position at which the track reaches column x_target.

        Returns (position, row_there). Raises if the column does not vary with
        stage position, which would make the inverse meaningless.
        """
        a, b = self.coef_x
        if a == 0 or not np.isfinite(a):
            raise ValueError("column does not vary with stage position; cannot invert")
        w = (x_target - b) / a
        return float(w), self.y_at(w)

    def rms(self):
        """RMS of the perpendicular residual, in pixels."""
        return float(np.sqrt(np.mean(self.residual_perp**2)))


def fit_track(positions, rows, cols):
    """Fit image row and column independently against stage position.

    Needs at least two points: a one-point fit is degenerate, and the notebook
    only got away with it because it suppressed OptimizeWarning globally.
    """
    w = np.asarray(positions, dtype=float)
    r = np.asarray(rows, dtype=float)
    c = np.asarray(cols, dtype=float)
    if w.size < 2:
        raise ValueError(f"need at least 2 frames to fit a line, got {w.size}")
    if len(np.unique(w)) < 2:
        raise ValueError("all frames are at the same stage position; cannot fit")

    coef_x, cov_x = np.polyfit(w, c, 1, cov=True)
    coef_y, cov_y = np.polyfit(w, r, 1, cov=True)

    fit = TrackFit(
        positions=w, rows=r, cols=c,
        coef_x=coef_x, cov_x=cov_x,
        coef_y=coef_y, cov_y=cov_y,
    )
    fit.residual_x = c - np.polyval(coef_x, w)
    fit.residual_y = r - np.polyval(coef_y, w)

    # Signed perpendicular distance from the fitted line. The notebook built the
    # sign from the degenerate t-fit coefficients, which is why that expression
    # looked arbitrary; here it uses the real ones. slope = d(row)/d(col).
    a_x = coef_x[0]
    if a_x != 0 and np.isfinite(a_x):
        slope = coef_y[0] / a_x
        sign = np.sign(r - slope * c - coef_y[1] + slope * coef_x[1])
    else:
        sign = np.sign(fit.residual_y)
    sign[sign == 0] = 1.0
    fit.residual_perp = sign * np.hypot(fit.residual_x, fit.residual_y)
    return fit


def evaluate(fit, w):
    """(x, y) = (column, row) of the fitted track at stage position w."""
    return fit.x_at(w), fit.y_at(w)
