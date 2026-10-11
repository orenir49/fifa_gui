"""Writing results out: CSV tables, a JSON fit record, and a reusable session.

The session file matters more than it looks: centroid QA is human judgement,
and without it that judgement evaporates when the window closes.
"""

import csv
import json
import os
from datetime import datetime, timezone

import numpy as np

FRAME_COLUMNS = [
    "file", "position", "repeat", "row", "col",
    "override_row", "override_col", "included",
    "residual_x", "residual_y", "residual_perp",
    "peak", "flags",
]


def _clean(value):
    if value is None:
        return ""
    if isinstance(value, float) and not np.isfinite(value):
        return ""
    return value


def write_frames_csv(path, records, fit=None):
    """One row per frame, measurements and judgements together."""
    resid_x = {}
    resid_y = {}
    if fit is not None:
        used = [r for r in records if r.usable]
        for rec, rx, ry in zip(used, fit.residual_x, fit.residual_y):
            resid_x[id(rec)] = float(rx)
            resid_y[id(rec)] = float(ry)

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(FRAME_COLUMNS)
        for rec in records:
            writer.writerow([
                rec.name,
                rec.position,
                _clean(rec.repeat),
                _clean(round(rec.row, 4) if np.isfinite(rec.row) else None),
                _clean(round(rec.col, 4) if np.isfinite(rec.col) else None),
                _clean(rec.override_row),
                _clean(rec.override_col),
                int(rec.include),
                _clean(round(resid_x.get(id(rec), float("nan")), 4)),
                _clean(round(resid_y.get(id(rec), float("nan")), 4)),
                _clean(round(rec.residual, 4) if np.isfinite(rec.residual) else None),
                _clean(round(rec.peak, 1) if np.isfinite(rec.peak) else None),
                " ".join(rec.all_flags),
            ])
    return path


def fit_summary(dataset, params):
    """The JSON record for one dataset: results plus everything needed to redo them."""
    fit = dataset.fit
    spec = params["stage_spec"]
    out = {
        "dataset": dataset.name,
        "directory": dataset.directory,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parameters": dict(params),
        "n_frames": len(dataset.records),
        "n_included": sum(1 for r in dataset.records if r.usable),
        "convention": "x is the image column, y is the image row",
    }
    if fit is None:
        out["error"] = dataset.error or "no fit"
        return out

    out.update({
        "coef_x": [float(v) for v in fit.coef_x],
        "coef_y": [float(v) for v in fit.coef_y],
        "cov_x": [[float(v) for v in r] for r in fit.cov_x],
        "cov_y": [[float(v) for v in r] for r in fit.cov_y],
        "stage_spec": spec,
        "x_at_spec": fit.x_at(spec),
        "y_at_spec": fit.y_at(spec),
        "sigma_x_at_spec": fit.sigma_x_at(spec),
        "sigma_y_at_spec": fit.sigma_y_at(spec),
        "rms_residual_px": fit.rms(),
        "rms_residual_um": fit.rms() * params["um_per_px"],
        "max_residual_px": float(np.max(np.abs(fit.residual_perp))),
    })
    try:
        pos, row = fit.position_for_x(params["x_target"])
        out["inverse"] = {"x_target": params["x_target"], "position": pos, "y": row}
    except ValueError as exc:
        out["inverse"] = {"error": str(exc)}
    return out


def write_fit_json(path, datasets, params, app_version=""):
    payload = {
        "app": "FIFA Calculate",
        "version": app_version,
        "datasets": [fit_summary(d, params) for d in datasets],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


def write_inverse_csv(path, datasets, x_target):
    """The cell-14 table: where each dataset reaches a target column."""
    rows = []
    for ds in datasets:
        if ds.fit is None:
            continue
        try:
            pos, y = ds.fit.position_for_x(x_target)
        except ValueError:
            continue
        rows.append((ds.name, float(ds.fit.coef_x[0]), float(ds.fit.coef_x[1]), pos, y))

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["dataset", "a_x", "b_x", "position", "y"])
        for name, a, b, pos, y in rows:
            writer.writerow([name, f"{a:.9g}", f"{b:.6f}", f"{pos:.1f}", f"{y:.2f}"])
        if rows:
            positions = [r[3] for r in rows]
            ys = [r[4] for r in rows]
            writer.writerow([])
            writer.writerow(["mean", "", "", f"{np.mean(positions):.1f}", f"{np.mean(ys):.2f}"])
            writer.writerow(["std", "", "", f"{np.std(positions):.1f}", f"{np.std(ys):.2f}"])
    return path


def write_session(path, datasets, params, app_version=""):
    """Enough to restore the analysis, including every manual judgement."""
    payload = {
        "app": "FIFA Calculate",
        "version": app_version,
        "saved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parameters": dict(params),
        "datasets": [
            {
                "directory": ds.directory,
                "frames": [
                    {
                        "file": rec.name,
                        "include": rec.include,
                        "override_row": rec.override_row,
                        "override_col": rec.override_col,
                    }
                    for rec in ds.records
                    if not rec.include or rec.overridden
                ],
            }
            for ds in datasets
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


def read_session(path):
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if payload.get("app") != "FIFA Calculate":
        raise ValueError(f"{os.path.basename(path)} is not a FIFA Calculate session.")
    return payload
