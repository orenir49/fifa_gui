"""Gathering unit entries from git, exported fits, and the open session."""

import json
import os

import pytest

from fifa_gui.core import unitsync
from fifa_gui.core.unittable import UnitEntry, UnitTable


def _entry(name, ax=-0.025, bx=12000.0, ay=0.0008, by=2800.0, updated="2026-01-01T00:00:00+00:00"):
    return UnitEntry(name=name, coef_x=(ax, bx), coef_y=(ay, by), updated_utc=updated)


# --------------------------------------------------------------- the target

def test_a_target_can_be_set_from_either_end():
    e = _entry("mast01")

    w, x, y = e.set_target_stage(300000)
    assert (w, x) == (300000.0, pytest.approx(ax_at(e, 300000)))
    assert e.target_from == "stage"
    assert e.has_target

    # Typing the column it reaches must solve back to the same stage.
    w2, x2, y2 = e.set_target_x(x)
    assert w2 == pytest.approx(300000)
    assert y2 == pytest.approx(y)
    assert e.target_from == "x"

    e.clear_target()
    assert not e.has_target
    assert e.target_stage is e.target_x is e.target_y is None


def ax_at(entry, w):
    return entry.coef_x[0] * w + entry.coef_x[1]


def test_the_target_survives_a_round_trip_through_the_file(tmp_path):
    table = UnitTable(tmp_path / "units.json")
    e = _entry("mast01")
    e.set_target_stage(300000)
    table.update(e)
    table.save()

    back = UnitTable(tmp_path / "units.json").load().units["mast01"]
    assert back.target_stage == pytest.approx(300000)
    assert back.target_x == pytest.approx(e.target_x)
    assert back.target_y == pytest.approx(e.target_y)
    assert back.target_from == "stage"


# ------------------------------------------------------------------ merging

def test_merge_keeps_the_newer_entry():
    table = UnitTable()
    table.update(_entry("mast01", bx=1.0, updated="2026-01-01T00:00:00+00:00"))

    added, updated = unitsync.merge(
        table, [_entry("mast01", bx=2.0, updated="2026-06-01T00:00:00+00:00")]
    )
    assert (added, updated) == (0, 1)
    assert table.units["mast01"].coef_x[1] == 2.0

    # An older one does not clobber it.
    unitsync.merge(table, [_entry("mast01", bx=3.0, updated="2025-01-01T00:00:00+00:00")])
    assert table.units["mast01"].coef_x[1] == 2.0


def test_merge_keeps_the_target_a_person_chose():
    """A fresher fit is a new measurement; the target is a decision."""
    table = UnitTable()
    old = _entry("mast01", updated="2026-01-01T00:00:00+00:00")
    old.set_target_stage(300000)
    table.update(old)

    fresh = _entry("mast01", bx=12345.0, updated="2026-06-01T00:00:00+00:00")
    unitsync.merge(table, [fresh])

    kept = table.units["mast01"]
    assert kept.coef_x[1] == 12345.0           # the new fit won
    assert kept.target_stage == pytest.approx(300000)
    assert kept.target_from == "stage"
    # ... and y was re-solved against the new coefficients, not carried over.
    assert kept.target_y == pytest.approx(kept.coef_y[0] * 300000 + kept.coef_y[1])


# ---------------------------------------------------------------- the files

def _write_fits_json(path, directory, generated="2026-05-05T00:00:00+00:00"):
    payload = {
        "app": "FIFA Calculate",
        "datasets": [{
            "dataset": "lab/mast07/1",
            "directory": directory,
            "generated_utc": generated,
            "coef_x": [-0.0249, 12194.9],
            "coef_y": [0.00078, 2870.8],
            "cov_x": [[1.1e-09, 0.0], [0.0, 105.4]],
            "cov_y": [[2.6e-09, 0.0], [0.0, 254.8]],
            "rms_residual_px": 1.95,
            "n_included": 6,
        }],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def test_exported_fits_are_found_under_the_data_root(tmp_path):
    _write_fits_json(str(tmp_path / "mast07" / "1" / "fits.json"),
                     str(tmp_path / "mast07" / "1"))
    entries = unitsync.scan_for_fit_exports([str(tmp_path)])

    assert [e.name for e in entries] == ["mast07"]
    e = entries[0]
    assert e.coef_x == (-0.0249, 12194.9)
    assert e.n_frames == 6
    assert e.sigma_x[1] == pytest.approx(105.4 ** 0.5)


def test_a_dataset_that_failed_to_fit_is_not_an_entry(tmp_path):
    path = tmp_path / "broken" / "fits.json"
    os.makedirs(path.parent, exist_ok=True)
    path.write_text(json.dumps({
        "datasets": [{"dataset": "lab/mast09/1",
                      "directory": str(tmp_path / "mast09" / "1"),
                      "error": "Need at least 2 included frames, have 1."}]
    }), encoding="utf-8")

    assert unitsync.scan_for_fit_exports([str(tmp_path)]) == []


def test_scanning_survives_a_corrupt_export(tmp_path):
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "fits.json").write_text("{not json", encoding="utf-8")
    _write_fits_json(str(tmp_path / "good" / "fits.json"), str(tmp_path / "mast07" / "1"))

    assert [e.name for e in unitsync.scan_for_fit_exports([str(tmp_path)])] == ["mast07"]


def test_a_missing_table_is_not_an_error(tmp_path):
    text, where = unitsync.fetch_table_json(tmp_path / "nope" / "units.json")
    assert (text, where) == ("", "missing")
    assert unitsync.entries_from_table_json("") == []


def test_the_working_tree_is_read_when_git_cannot_help(tmp_path, monkeypatch):
    """No repository, no network: the file on disk still has to be read."""
    table = UnitTable(tmp_path / "units.json")
    table.update(_entry("mast01"))
    table.save()

    monkeypatch.setattr(unitsync, "repo_root", lambda start: None)
    text, where = unitsync.fetch_table_json(tmp_path / "units.json")
    assert where == "working tree"
    assert [e.name for e in unitsync.entries_from_table_json(text)] == ["mast01"]
