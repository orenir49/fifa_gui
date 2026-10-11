"""Loading frames and parsing the stage position out of their filenames.

Every consumer goes through `load_frame`, so there is exactly one place that
knows about the file formats, and it always takes a full path.
"""

import re
from os import path, listdir

import numpy as np

# "324999.npy", "324998_0.npy" (position with a repeat index), "+111989.jpg",
# "-20.png".  Note that int("324998_0") is 3249980 in Python -- underscores are
# valid digit separators -- so the suffix must be split off explicitly.
POSITION_RE = re.compile(r"^([+-]?\d+)(?:_(\d+))?$")

NUMPY_EXT = (".npy",)
FITS_EXT = (".fit", ".fits")
PIL_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")
FRAME_EXT = NUMPY_EXT + FITS_EXT + PIL_EXT


def position_from_filename(name):
    """Parse "<position>[_<repeat>].<ext>" into (position, repeat).

    `repeat` is None when the name carries no suffix. Returns None if the stem
    is not a position at all, so callers can skip stray files.
    """
    stem = path.splitext(path.basename(name))[0]
    m = POSITION_RE.match(stem)
    if m is None:
        return None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) is not None else None)


def load_frame(filepath, mmap=False):
    """Load a frame as a 2-D ndarray. Raises ValueError on an unknown extension.

    With mmap=True a .npy is memory-mapped instead of read, so a caller that
    only needs a window of a 93 MB frame touches only those pages.
    """
    ext = path.splitext(filepath)[1].lower()

    if ext in NUMPY_EXT:
        img = np.load(filepath, mmap_mode="r" if mmap else None)
    elif ext in FITS_EXT:
        from astropy.io import fits  # lazy: Calibrate never bundles astropy

        with fits.open(filepath) as hd:
            img = hd[0].data
    elif ext in PIL_EXT:
        from PIL import Image  # lazy, for the same reason

        with Image.open(filepath) as im:
            img = np.array(im)
    else:
        raise ValueError(f"unsupported frame format: {filepath}")

    if img is None:
        raise ValueError(f"no image data in {filepath}")
    img = np.asarray(img)
    if img.ndim == 3:
        img = img[:, :, 0]  # colour source: the notebook used the first channel
    if img.ndim != 2:
        raise ValueError(f"expected a 2-D frame, got shape {img.shape} in {filepath}")
    return img


def full_scale(dtype):
    """Nominal maximum value for an image dtype, for the saturation test."""
    if np.issubdtype(dtype, np.integer):
        return float(np.iinfo(dtype).max)
    return 1.0


def scan_directory(directory):
    """Frames in `directory`, as (fullpath, position, repeat), sorted by position.

    Files whose name is not a position are skipped rather than raising -- a data
    directory routinely picks up a log or a stray export.
    """
    out = []
    for name in listdir(directory):
        if path.splitext(name)[1].lower() not in FRAME_EXT:
            continue
        parsed = position_from_filename(name)
        if parsed is None:
            continue
        out.append((path.join(directory, name), parsed[0], parsed[1]))
    out.sort(key=lambda t: (t[1], -1 if t[2] is None else t[2]))
    return out


def decimate(image, factor):
    """Strided decimation, contiguous so it can be wrapped in a QImage."""
    return np.ascontiguousarray(image[::factor, ::factor])


def build_pyramid(image, factors=(8, 4)):
    """Decimation levels kept for display after the full frame is discarded."""
    return {f: decimate(image, f) for f in factors}


def crop(image, row, col, size):
    """A `size`x`size` full-resolution cutout centred on (row, col).

    Returns (cutout, row0, col0) where (row0, col0) is its origin in the full
    frame, so the caller can map cutout coordinates back.
    """
    half = size // 2
    r0 = int(np.clip(round(row) - half, 0, max(image.shape[0] - size, 0)))
    c0 = int(np.clip(round(col) - half, 0, max(image.shape[1] - size, 0)))
    return (
        np.ascontiguousarray(image[r0 : r0 + size, c0 : c0 + size]),
        r0,
        c0,
    )
