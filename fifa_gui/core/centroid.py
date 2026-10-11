"""Locating the fiber spot in a frame.

Everything here works in (row, col) order. The notebook used
`photutils.centroid_com`, which returns (column, row), and unpacked it the other
way round -- so the sub-pixel refinement landed on the wrong axis, shifting the
reported fiber position by up to ~1 px. Computing the centre of mass here, in
numpy's own axis order, means that unpacking site no longer exists.
"""

from dataclasses import dataclass

import numpy as np

from fifa_gui.core.imageio import full_scale

#: fraction of full scale above which a pixel counts as saturated
SATURATION_FRACTION = 0.98

DEFAULT_HALF_WIDTH = 4   # the notebook's int(5 / 2.35) * 2, for a 5 um fiber
DEFAULT_BG_OFFSET = 100  # rows below the spot for the background box


@dataclass
class CentroidResult:
    """Everything the QA view needs to explain a measurement."""

    row: float
    col: float
    roi_bounds: tuple        # (r0, r1, c0, c1) in full-frame coordinates
    bkg_bounds: tuple
    bkg_median: float
    peak: float              # raw peak value, before background subtraction
    saturated: bool
    clamped: bool            # an ROI or background box hit the frame edge
    bkg_fallback: bool       # the background box had to be moved
    ok: bool
    message: str

    @property
    def flags(self):
        out = []
        if self.saturated:
            out.append("SAT")
        if self.clamped:
            out.append("EDGE")
        if self.bkg_fallback:
            out.append("BKG?")
        if not self.ok:
            out.append("FAIL")
        return out


def center_of_mass(data):
    """Flux-weighted centre of a 2-D array, as (row, col) offsets within it.

    Equivalent to photutils.centroid_com, but in numpy axis order. Returns
    (nan, nan) when the total is non-positive, which is what a background-
    dominated or empty ROI gives.
    """
    data = np.asarray(data, dtype=float)
    total = data.sum()
    if not np.isfinite(total) or total <= 0:
        return float("nan"), float("nan")
    row = float(data.sum(axis=1) @ np.arange(data.shape[0]) / total)
    col = float(data.sum(axis=0) @ np.arange(data.shape[1]) / total)
    return row, col


def _clamp_box(shape, r0, c0, size):
    """Clamp a size x size box to the frame. Returns (r0, r1, c0, c1, clamped)."""
    size = min(size, shape[0], shape[1])
    cr0 = int(np.clip(r0, 0, shape[0] - size))
    cc0 = int(np.clip(c0, 0, shape[1] - size))
    return cr0, cr0 + size, cc0, cc0 + size, (cr0 != r0 or cc0 != c0)


def find_centroid(
    image,
    half_width=DEFAULT_HALF_WIDTH,
    bg_offset=DEFAULT_BG_OFFSET,
    clip_negative=True,
    seed=None,
):
    """Sub-pixel centroid of the fiber spot, as a CentroidResult in (row, col).

    half_width    ROI half-size in pixels; the ROI is 2*half_width square.
    bg_offset     rows away from the spot for an identically sized background box.
    clip_negative clamp the background-subtracted ROI at zero. A centre of mass
                  with negative weights can land outside the data entirely, so
                  this defaults on.
    seed          (row, col) to centre the ROI on instead of the global maximum.
                  The brightest pixel in a 46 MP frame is not always the fiber --
                  a cosmic ray or a reflection steals it -- so the QA view lets
                  the operator re-seed from a click.
    """
    image = np.asarray(image)
    size = int(2 * half_width)
    if size < 2:
        raise ValueError(f"half_width={half_width} gives a {size}px ROI; use >= 1")

    if seed is None:
        r_peak, c_peak = np.unravel_index(np.argmax(image), image.shape)
    else:
        r_peak = int(np.clip(round(seed[0]), 0, image.shape[0] - 1))
        c_peak = int(np.clip(round(seed[1]), 0, image.shape[1] - 1))

    peak = float(image[r_peak, c_peak])
    saturated = peak >= SATURATION_FRACTION * full_scale(image.dtype)

    r0, r1, c0, c1, clamped = _clamp_box(
        image.shape, r_peak - half_width, c_peak - half_width, size
    )
    roi = np.asarray(image[r0:r1, c0:c1], dtype=float)

    # Background: an identically sized box offset along rows. The notebook only
    # ever looked below; near the bottom edge that silently wrapped, so try the
    # other side before giving up.
    bkg_fallback = False
    br0, br1, bc0, bc1, bclamped = _clamp_box(image.shape, r0 + bg_offset, c0, size)
    if bclamped:
        alt = _clamp_box(image.shape, r0 - bg_offset, c0, size)
        if not alt[4]:
            br0, br1, bc0, bc1, bclamped = alt
        bkg_fallback = True
    bkg_median = float(np.median(image[br0:br1, bc0:bc1]))

    roi = roi - bkg_median
    if clip_negative:
        roi = np.clip(roi, 0, None)

    d_row, d_col = center_of_mass(roi)
    ok = np.isfinite(d_row) and np.isfinite(d_col)

    return CentroidResult(
        row=d_row + r0 if ok else float("nan"),
        col=d_col + c0 if ok else float("nan"),
        roi_bounds=(r0, r1, c0, c1),
        bkg_bounds=(br0, br1, bc0, bc1),
        bkg_median=bkg_median,
        peak=peak,
        saturated=bool(saturated),
        clamped=bool(clamped or bclamped),
        bkg_fallback=bkg_fallback,
        ok=bool(ok),
        message="" if ok else "ROI has no positive flux after background subtraction",
    )
