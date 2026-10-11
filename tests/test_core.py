"""Core analysis tests, including golden values measured from the real data.

The golden values pin the port against the notebook: they are the notebook's own
printed numbers, shifted by exactly the centroid axis-swap correction and
nothing else. A larger difference means something else changed too.

Run:  .venv\\Scripts\\python -m pytest tests -q
Tests that need the lab data skip automatically when it is not mounted.
"""

import os

import numpy as np
import pytest

from fifa_gui.core.centroid import center_of_mass, find_centroid
from fifa_gui.core.flexure import flexure_stats
from fifa_gui.core.imageio import load_frame, position_from_filename, scan_directory
from fifa_gui.core.linefit import fit_track
from fifa_gui.core.sequence import (
    CameraSettings, SequencePlan, generate_targets, validate,
)

LAB = r"C:\Users\oreni\Documents\code\lab"
needs_lab = pytest.mark.skipif(not os.path.isdir(LAB), reason="lab data not available")

# (directory, x(300000), y(300000)) -- the notebook printed 4569.6/2812.9 and
# 4394.8/2537.5 with the axis swap in place.
GOLDEN = [
    (os.path.join(LAB, "mast03", "1"), 4569.71, 2812.83),
    (os.path.join(LAB, "mast04", "1"), 4395.82, 2536.49),
]


# --------------------------------------------------------------------- imageio

def test_position_parses_repeat_suffix_instead_of_swallowing_it():
    # int("324998_0") is 3249980 in Python: underscores are digit separators, so
    # the notebook's int(name.split('.')[0]) corrupted every flexure filename.
    assert position_from_filename("324998_0.npy") == (324998, 0)
    assert position_from_filename("284999.npy") == (284999, None)
    assert position_from_filename("-20.png") == (-20, None)
    assert position_from_filename("+111989.jpg") == (111989, None)


def test_non_position_filenames_are_skipped_not_fatal():
    assert position_from_filename("acquisition.log") is None
    assert position_from_filename("run.json") is None


# -------------------------------------------------------------------- centroid

def test_center_of_mass_is_row_col_not_col_row():
    a = np.zeros((9, 9))
    a[2, 7] = 1.0
    assert center_of_mass(a) == pytest.approx((2.0, 7.0))


def test_center_of_mass_matches_photutils_after_accounting_for_axis_order():
    photutils = pytest.importorskip("photutils.centroids")
    rng = np.random.default_rng(0)
    data = rng.random((17, 23)) + 5.0
    ref_col, ref_row = photutils.centroid_com(data)   # photutils returns (x, y)
    row, col = center_of_mass(data)
    assert (row, col) == pytest.approx((ref_row, ref_col), abs=1e-10)


def test_negative_pixels_drag_the_centre_of_mass_off_the_spot():
    # Negative weights pull the centre of mass away from the actual flux: here
    # the only bright pixel is at (1, 1) but the result is (0, 0). This is why
    # find_centroid clips at zero by default.
    a = np.array([[0.0, 0, 0], [0, 10, 0], [0, 0, -5]])
    assert center_of_mass(a) == pytest.approx((0.0, 0.0))
    assert center_of_mass(np.clip(a, 0, None)) == pytest.approx((1.0, 1.0))


def test_non_positive_total_is_refused_rather_than_guessed():
    # photutils returns a number here; a NaN that find_centroid turns into a
    # FAIL flag is more useful than a centroid outside the data.
    a = np.array([[0.0, 0, 0], [0, 10, 0], [0, 0, -30]])
    assert all(np.isnan(v) for v in center_of_mass(a))

    # A background box brighter than the ROI drives the subtracted total
    # negative; the result is a FAIL flag, not a plausible-looking number.
    # Seed the ROI on a dim spot whose background box is far brighter: the
    # subtracted total goes negative and the result is a FAIL flag, not a
    # plausible-looking number.
    img = np.zeros((40, 40), dtype=np.int16)
    img[20, 20] = 5
    img[24:32, 16:24] = 100      # the whole background box, so its median moves
    res = find_centroid(img, half_width=4, bg_offset=8, clip_negative=False,
                        seed=(20, 20))
    assert res.bkg_median == 100
    assert not res.ok and "FAIL" in res.flags
    assert np.isnan(res.row) and np.isnan(res.col)


def test_roi_at_the_frame_edge_is_clamped_not_wrapped():
    img = np.zeros((40, 40), dtype=np.uint16)
    img[1, 1] = 1000
    res = find_centroid(img, half_width=4, bg_offset=10)
    assert res.clamped
    r0, r1, c0, c1 = res.roi_bounds
    assert r0 >= 0 and c0 >= 0 and r1 <= 40 and c1 <= 40
    assert 0 <= res.row < 40 and 0 <= res.col < 40


def test_saturation_is_flagged():
    img = np.zeros((40, 40), dtype=np.uint16)
    img[20, 20] = 65520        # 12-bit full well, scaled into 16 bits
    assert find_centroid(img, bg_offset=10).saturated
    img[20, 20] = 30000
    assert not find_centroid(img, bg_offset=10).saturated


def test_seed_overrides_the_global_maximum():
    img = np.zeros((80, 80), dtype=np.uint16)
    img[10, 10] = 60000        # a cosmic ray stealing the argmax
    img[50, 50] = 9000         # the real fiber
    auto = find_centroid(img, bg_offset=20)
    seeded = find_centroid(img, bg_offset=20, seed=(50, 50))
    assert auto.row == pytest.approx(10, abs=1)
    assert (seeded.row, seeded.col) == pytest.approx((50, 50), abs=1)


# --------------------------------------------------------------------- linefit

def test_fit_needs_at_least_two_distinct_positions():
    with pytest.raises(ValueError):
        fit_track([1.0], [2.0], [3.0])
    with pytest.raises(ValueError):
        fit_track([5.0, 5.0], [1.0, 2.0], [3.0, 4.0])


def test_polyfit_matches_curve_fit():
    # Why scipy is not a dependency: on this linear model polyfit agrees with
    # curve_fit to ~1e-9 relative and returns the covariance as well.
    scipy_opt = pytest.importorskip("scipy.optimize")
    w = np.array([285000.0, 295000, 305000, 315000, 325000, 330000])
    y = np.array([2516.1, 2520.3, 2524.9, 2529.2, 2533.8, 2536.1])
    ref, _ = scipy_opt.curve_fit(lambda x, a, b: a * x + b, w, y)
    got, _ = np.polyfit(w, y, 1, cov=True)
    assert got == pytest.approx(ref, rel=1e-8)


def test_fit_recovers_a_known_line_and_inverts_it():
    w = np.arange(285000, 335000, 10000, dtype=float)
    cols = 3.0e-3 * w + 100.0
    rows = -1.0e-3 * w + 50.0
    fit = fit_track(w, rows, cols)
    assert fit.x_at(300000) == pytest.approx(3.0e-3 * 300000 + 100.0)
    assert fit.rms() == pytest.approx(0.0, abs=1e-6)
    pos, row = fit.position_for_x(fit.x_at(312345))
    assert pos == pytest.approx(312345, rel=1e-9)
    assert row == pytest.approx(fit.y_at(312345))


# --------------------------------------------------------------------- flexure

def test_flexure_circle_is_a_diameter():
    # The notebook drew plt.Circle(centre, 30 / 2.35) -- a 30 um RADIUS -- while
    # labelling it "30 um diameter". The label was the intent.
    st = flexure_stats([10.0, 11.0], [10.0, 10.0], um_per_px=2.35, diameter_um=30.0)
    assert st.radius_px == pytest.approx(15.0 / 2.35)
    assert flexure_stats([10.0], [10.0], 2.35, diameter_um=60.0).radius_px == \
        pytest.approx(30.0 / 2.35)


def test_flexure_pass_fail_against_the_circle():
    rows = [100.0, 100.0, 100.0]
    st = flexure_stats(rows, [100.0, 101.0, 99.0], um_per_px=2.35)
    assert st.max_px == pytest.approx(1.0)
    assert st.passed
    st = flexure_stats(rows, [100.0, 130.0, 70.0], um_per_px=2.35)
    assert not st.passed


# -------------------------------------------------------------------- sequence

def test_generate_targets_matches_the_notebook_sweep():
    assert generate_targets(285000, 330000, 10000) == [285000, 295000, 305000, 315000, 325000]
    assert generate_targets(100, 80, -10) == [100, 90, 80]
    with pytest.raises(ValueError):
        generate_targets(0, 100, -10)


def test_validate_catches_the_backoff_driving_into_the_left_limit(tmp_path):
    # The target itself is legal (110000 is inside [6639, 334911]); it is the
    # retraction to 0 that would drive into the limit. That is the check the
    # notebook had no equivalent of.
    plan = SequencePlan(targets=[110000], out_dir=str(tmp_path), backoff=110000)
    errs = [p.text for p in validate(plan, limits=(6639, 334911)) if p.level == "error"]
    assert any("back-off" in e for e in errs)
    plan.targets = [285000]
    assert not [p for p in validate(plan, limits=(6639, 334911)) if p.level == "error"]


def test_validate_flags_a_missing_output_directory():
    plan = SequencePlan(targets=[285000], out_dir=r"Z:\nope")
    assert any(p.level == "error" for p in validate(plan))


def test_filename_follows_the_achieved_position():
    plan = SequencePlan(repeats=1)
    assert plan.filename(284999, 0) == "284999.npy"
    plan.repeats = 5
    assert plan.filename(324998, 3) == "324998_3.npy"


def test_plan_round_trips_through_a_dict():
    plan = SequencePlan(targets=[1, 2], out_dir="x", camera=CameraSettings(gain=99))
    back = SequencePlan.from_dict(plan.to_dict())
    assert back.targets == [1, 2] and back.camera.gain == 99


# ------------------------------------------------------- golden, against the lab data

@needs_lab
@pytest.mark.parametrize("directory,x_expected,y_expected", GOLDEN)
def test_fit_reproduces_the_notebook_result(directory, x_expected, y_expected):
    frames = scan_directory(directory)
    assert len(frames) >= 2
    w, rows, cols = [], [], []
    for p, pos, _ in frames:
        res = find_centroid(load_frame(p))
        w.append(pos)
        rows.append(res.row)
        cols.append(res.col)
    fit = fit_track(w, rows, cols)
    assert fit.x_at(300000) == pytest.approx(x_expected, abs=0.01)
    assert fit.y_at(300000) == pytest.approx(y_expected, abs=0.01)


@needs_lab
def test_saturation_distinguishes_the_archived_datasets():
    # Every mast03 frame is at the 12-bit full well; mast04 is clean.
    def all_saturated(d):
        return [find_centroid(load_frame(p)).saturated for p, _, _ in scan_directory(d)]

    assert all(all_saturated(os.path.join(LAB, "mast03", "1")))
    assert not any(all_saturated(os.path.join(LAB, "mast04", "1")))


@needs_lab
def test_flexure_directory_parses_as_repeats_of_one_position():
    frames = scan_directory(os.path.join(LAB, "mast00", "flex"))
    assert len(frames) == 5
    assert {pos for _, pos, _ in frames} == {324998}
    assert sorted(rep for _, _, rep in frames) == [0, 1, 2, 3, 4]
