"""Synthetic frames, so the GUI tests do not re-read gigabytes of real data.

Each frame holds a Gaussian spot whose position is a known linear function of
the stage position, which makes the expected fit exact rather than merely
plausible. The real-data golden values live in test_core.py.
"""

import os

import numpy as np
import pytest

# Ground truth of the synthetic track, chosen so the spot stays well inside the
# frame over the whole sweep: col runs 100 -> 145, row runs 200 -> 177.5.
TRUE_COL_SLOPE = 1.0e-3
TRUE_COL_INTERCEPT = -185.0
TRUE_ROW_SLOPE = -5.0e-4
TRUE_ROW_INTERCEPT = 342.5

POSITIONS = [285000, 295000, 305000, 315000, 325000, 330000]
#: tall enough that the default 100-row background offset stays on the frame
SHAPE = (500, 400)
BIAS = 650
PEAK = 30000
SIGMA = 1.2


def true_col(position):
    return TRUE_COL_SLOPE * position + TRUE_COL_INTERCEPT


def true_row(position):
    return TRUE_ROW_SLOPE * position + TRUE_ROW_INTERCEPT


def make_frame(row, col, peak=PEAK, shape=SHAPE, seed=0):
    """A bias-plus-noise frame with one Gaussian spot at (row, col)."""
    rng = np.random.default_rng(seed)
    rr, cc = np.mgrid[0:shape[0], 0:shape[1]]
    img = BIAS + rng.normal(0, 8, shape)
    img += peak * np.exp(-((rr - row) ** 2 + (cc - col) ** 2) / (2 * SIGMA**2))
    return np.clip(img, 0, 65535).astype(np.uint16)


def write_dataset(directory, positions=POSITIONS, peak=PEAK, repeats=None):
    os.makedirs(directory, exist_ok=True)
    for i, position in enumerate(positions):
        row, col = true_row(position), true_col(position)
        if repeats is None:
            np.save(os.path.join(directory, f"{position}.npy"),
                    make_frame(row, col, peak, seed=i))
        else:
            for k in range(repeats):
                np.save(os.path.join(directory, f"{position}_{k}.npy"),
                        make_frame(row, col, peak, seed=i * 100 + k))
    return directory


@pytest.fixture(scope="session")
def synthetic_root(tmp_path_factory):
    return tmp_path_factory.mktemp("frames")


@pytest.fixture(scope="session")
def dataset_a(synthetic_root):
    return write_dataset(str(synthetic_root / "runA"))


@pytest.fixture(scope="session")
def dataset_b(synthetic_root):
    # A second, slightly offset run, so cross-dataset features have two rows.
    directory = str(synthetic_root / "runB")
    os.makedirs(directory, exist_ok=True)
    for i, position in enumerate(POSITIONS):
        np.save(
            os.path.join(directory, f"{position}.npy"),
            make_frame(true_row(position) + 3.0, true_col(position) - 2.0, seed=i + 50),
        )
    return directory


@pytest.fixture(scope="session")
def dataset_saturated(synthetic_root):
    return write_dataset(str(synthetic_root / "runSat"), peak=70000)


@pytest.fixture(scope="session")
def dataset_flex(synthetic_root):
    return write_dataset(str(synthetic_root / "runFlex"), positions=[324998], repeats=5)
