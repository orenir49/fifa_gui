"""Calculate app behaviour, driven headlessly against small synthetic frames.

The real-data golden values are asserted in test_core.py; re-reading 93 MB
frames once per test here would make the suite slow and would compete with
whatever the bench is writing to the same disk.
"""

import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "QtAgg")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fifa_gui.apps.calculate.window import CalculateWindow  # noqa: E402
from tests.conftest import POSITIONS, true_col, true_row  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.IniFormat)
    win = CalculateWindow()
    win.setAttribute(Qt.WA_DeleteOnClose, True)
    yield win
    # Destroy it for real: merely closing leaves the window, its thread pool and
    # its matplotlib canvases alive for every later processEvents() to service.
    win.close()
    win.deleteLater()
    qapp.processEvents()


def _load(window, qapp, directory, timeout_s=30):
    window.load_directory(directory)
    deadline = time.monotonic() + timeout_s
    while sum(window._pending.values()) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert sum(window._pending.values()) == 0, "frames did not finish loading"
    window._refit_all()
    qapp.processEvents()


# ------------------------------------------------------------------ loading

def test_window_opens_with_no_data(window):
    assert window.datasets == []
    assert "No fit yet." in window.result_label.text()


def test_fit_recovers_the_known_track(window, qapp, dataset_a):
    _load(window, qapp, dataset_a)
    ds = window.datasets[0]
    assert len(ds.records) == len(POSITIONS)
    assert ds.fit is not None
    # The spot really is at true_row/true_col, so the fit must land on them.
    assert ds.fit.x_at(300000) == pytest.approx(true_col(300000), abs=0.05)
    assert ds.fit.y_at(300000) == pytest.approx(true_row(300000), abs=0.05)
    assert ds.fit.rms() < 0.2


def test_frames_are_not_retained_at_full_size(window, qapp, dataset_a):
    # Real frames are 93 MB each and mast00 is 5.9 GB, so a record must keep
    # only the pyramid and the cutout.
    _load(window, qapp, dataset_a)
    rec = window.datasets[0].records[0]
    assert rec.thumb is not None and rec.cutout is not None
    assert not hasattr(rec, "image")


def test_loading_the_same_directory_twice_is_refused(window, qapp, dataset_a):
    _load(window, qapp, dataset_a)
    window.load_directory(dataset_a)
    assert len(window.datasets) == 1


def test_directory_with_no_frames_is_reported(window, qapp, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "fifa_gui.apps.calculate.window.QMessageBox.information",
        lambda *a, **k: seen.setdefault("called", True),
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    window.load_directory(str(empty))
    assert seen.get("called") and window.datasets == []


# ------------------------------------------------------------- centroid QA

def test_excluding_a_frame_changes_the_fit(window, qapp, dataset_a):
    _load(window, qapp, dataset_a)
    ds = window.datasets[0]
    before = ds.fit.x_at(300000)
    n_before = sum(1 for r in ds.records if r.usable)

    window.table_model.setData(
        window.table_model.index(0, 0), Qt.Unchecked, Qt.CheckStateRole
    )
    window._refit_all()

    assert sum(1 for r in ds.records if r.usable) == n_before - 1
    assert ds.fit.x_at(300000) != pytest.approx(before, abs=1e-9)


def test_a_fit_needs_two_included_frames(window, qapp, dataset_a):
    _load(window, qapp, dataset_a)
    ds = window.datasets[0]
    for row in range(len(ds.records) - 1):
        window.table_model.setData(
            window.table_model.index(row, 0), Qt.Unchecked, Qt.CheckStateRole
        )
    window._refit_all()
    assert ds.fit is None
    assert "at least 2" in ds.error


def test_manual_override_flags_the_frame_and_refits(window, qapp, dataset_a):
    _load(window, qapp, dataset_a)
    ds = window.datasets[0]
    window.table.selectRow(0)
    qapp.processEvents()
    rec = window._current_record
    assert rec is not None and not rec.overridden

    before = ds.fit.x_at(300000)
    window._on_view_clicked(rec.row + 4.0, rec.col + 4.0)

    assert rec.overridden
    assert "MANUAL" in rec.all_flags
    assert ds.fit.x_at(300000) != pytest.approx(before, abs=1e-9)

    window._revert_override()
    assert not rec.overridden
    assert ds.fit.x_at(300000) == pytest.approx(before, abs=1e-9)


def test_override_seeds_the_centroid_away_from_the_global_maximum(window, qapp, dataset_a):
    # The point of re-centroiding from a click: np.argmax over a 46 MP frame is
    # fragile, so the operator must be able to say where the fiber really is.
    _load(window, qapp, dataset_a)
    window.table.selectRow(0)
    qapp.processEvents()
    rec = window._current_record
    window._on_view_clicked(rec.row + 30.0, rec.col + 30.0)
    assert rec.overridden
    # It re-centroids near the click, not back at the bright spot.
    assert abs(rec.override_row - rec.row) > 5


def test_saturated_frames_are_flagged(window, qapp, dataset_saturated):
    _load(window, qapp, dataset_saturated)
    assert all("SAT" in r.all_flags for r in window.datasets[0].records)


def test_clean_frames_are_not_flagged(window, qapp, dataset_a):
    _load(window, qapp, dataset_a)
    assert not any("SAT" in r.all_flags for r in window.datasets[0].records)


# ----------------------------------------------------------------- flexure

def test_flexure_tab_groups_repeats(window, qapp, dataset_flex):
    _load(window, qapp, dataset_flex)
    window._refresh_flexure()
    text = window.flex_label.text()
    assert "n = 5" in text
    assert "PASS" in text or "FAIL" in text


def test_flexure_circle_follows_the_diameter_field(window, qapp, dataset_flex):
    from fifa_gui.core.flexure import flexure_stats

    _load(window, qapp, dataset_flex)
    window.um_per_px.setValue(2.35)
    window.flex_diameter.setValue(30.0)
    used = [r for r in window.datasets[0].records if r.usable]
    stats = flexure_stats(
        [r.effective_row for r in used], [r.effective_col for r in used],
        um_per_px=2.35, diameter_um=30.0,
    )
    assert stats.radius_px == pytest.approx(15.0 / 2.35)


# ----------------------------------------------------------------- inverse

def test_inverse_table_averages_across_datasets(window, qapp, dataset_a, dataset_b):
    _load(window, qapp, dataset_a)
    _load(window, qapp, dataset_b)
    target = true_col(310000)
    window.x_target.setValue(target)
    window._refresh_inverse()
    text = window.inverse_label.text()
    assert "mean" in text and "std" in text
    # runA's fit is exact, so it must invert back to the position it came from.
    pos, _ = window.datasets[0].fit.position_for_x(target)
    assert pos == pytest.approx(310000, rel=1e-3)


# ------------------------------------------------------------------ export

def test_export_writes_every_artifact(window, qapp, dataset_a, dataset_b,
                                      tmp_path, monkeypatch):
    _load(window, qapp, dataset_a)
    _load(window, qapp, dataset_b)

    monkeypatch.setattr(
        "fifa_gui.apps.calculate.window.QFileDialog.getExistingDirectory",
        lambda *a, **k: str(tmp_path),
    )
    monkeypatch.setattr(
        "fifa_gui.apps.calculate.window.QMessageBox.information", lambda *a, **k: None
    )
    window.export_results()

    names = {p.name for p in tmp_path.iterdir()}
    assert {"fits.json", "inverse.csv", "session.json",
            "track.png", "residuals.png", "flexure.png"} <= names
    assert sum(1 for n in names if n.endswith("_frames.csv")) == 2

    payload = json.loads((tmp_path / "fits.json").read_text())
    assert len(payload["datasets"]) == 2
    first = payload["datasets"][0]
    assert first["x_at_spec"] == pytest.approx(true_col(300000), abs=0.05)
    assert first["parameters"]["stage_spec"] == 300000
    assert "inverse" in first
    assert first["convention"].startswith("x is the image column")

    for png in ("track.png", "residuals.png", "flexure.png"):
        assert (tmp_path / png).stat().st_size > 5000


def test_exported_csv_carries_the_judgements(window, qapp, dataset_a,
                                             tmp_path, monkeypatch):
    _load(window, qapp, dataset_a)
    window.table_model.setData(
        window.table_model.index(2, 0), Qt.Unchecked, Qt.CheckStateRole
    )
    window._refit_all()

    monkeypatch.setattr(
        "fifa_gui.apps.calculate.window.QFileDialog.getExistingDirectory",
        lambda *a, **k: str(tmp_path),
    )
    monkeypatch.setattr(
        "fifa_gui.apps.calculate.window.QMessageBox.information", lambda *a, **k: None
    )
    window.export_results()

    csv_path = next(p for p in tmp_path.iterdir() if p.name.endswith("_frames.csv"))
    lines = csv_path.read_text().strip().splitlines()
    assert lines[0].startswith("file,position,repeat")
    included = [line.split(",")[7] for line in lines[1:]]
    assert included.count("0") == 1


def test_session_round_trip_restores_judgements(window, qapp, dataset_a,
                                                tmp_path, monkeypatch):
    _load(window, qapp, dataset_a)
    ds = window.datasets[0]
    window.table_model.setData(
        window.table_model.index(1, 0), Qt.Unchecked, Qt.CheckStateRole
    )
    excluded = ds.records[1].name
    window.table.selectRow(3)
    qapp.processEvents()
    overridden_rec = window._current_record
    window._on_view_clicked(overridden_rec.row + 4.0, overridden_rec.col + 4.0)
    overridden = overridden_rec.name
    window._refit_all()

    session = tmp_path / "session.json"
    from fifa_gui.core import export as core_export

    core_export.write_session(str(session), window.datasets, window._params(), "test")

    monkeypatch.setattr(
        "fifa_gui.apps.calculate.window.QFileDialog.getOpenFileName",
        lambda *a, **k: (str(session), ""),
    )
    window.open_session()
    deadline = time.monotonic() + 30
    while sum(window._pending.values()) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    window._refit_all()

    restored = {r.name: r for r in window.datasets[0].records}
    assert restored[excluded].include is False
    assert restored[overridden].overridden


# --------------------------------------------------------------- unit table

def test_unit_table_update_and_interpolate(window, qapp, dataset_a, tmp_path):
    from fifa_gui.core.unittable import UnitTable

    path = tmp_path / "units.json"
    window.unit_table = UnitTable(path)
    _load(window, qapp, dataset_a)
    window.tree.setCurrentItem(window.tree.topLevelItem(0))
    window.unit_name.setText("mast01")
    window.update_unit_from_dataset()

    saved = UnitTable(path).load()
    entry = saved.units["mast01"]
    fit = window.datasets[0].fit
    assert entry.coef_x == pytest.approx(tuple(fit.coef_x))
    assert entry.xy_at(300000) == pytest.approx((fit.x_at(300000), fit.y_at(300000)))

    # inverse round trip, and the GUI table fills the interpolated columns
    x, y = entry.xy_at(250000)
    w, y2 = entry.stage_for_x(x)
    assert w == pytest.approx(250000) and y2 == pytest.approx(y)
    window.interp_mode.setCurrentIndex(1)
    window.interp_value.setValue(250000)
    assert window.units_table.item(0, 10).text() == f"{x:.2f}"

    # updating again replaces, never duplicates
    window.update_unit_from_dataset()
    assert len(window.unit_table.units) == 1
