"""The running table of fit results, one row per unit (mast01, mast02, ...).

Plain JSON so it diffs cleanly in git. Each unit keeps its latest linear fit,
col(w) = ax*w + bx and row(w) = ay*w + by, where w is the stage position. As
everywhere else, **x is the image column and y is the image row**.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = 1


def _opt_float(v):
    return None if v is None else float(v)


@dataclass
class UnitEntry:
    name: str
    coef_x: tuple            # (slope, intercept) of column vs stage position
    coef_y: tuple            # (slope, intercept) of row vs stage position
    sigma_x: tuple = (0.0, 0.0)   # 1-sigma of each coefficient
    sigma_y: tuple = (0.0, 0.0)
    rms_px: float = float("nan")
    n_frames: int = 0
    source: str = ""
    updated_utc: str = ""
    #: The one stage position this unit is reported at. Either the stage or
    #: the column is chosen per unit, and the other two follow from the fit;
    #: they are stored so the table in git carries the answer, not just the
    #: coefficients to recompute it from.
    target_stage: float = None
    target_x: float = None
    target_y: float = None
    target_from: str = ""        # "stage" or "x": which one was typed

    # -- evaluation ---------------------------------------------------------

    def xy_at(self, w):
        """(x, y) = (column, row) at stage position w."""
        return (self.coef_x[0] * w + self.coef_x[1],
                self.coef_y[0] * w + self.coef_y[1])

    def stage_for_x(self, x):
        """Stage position where the column reaches x, and y there."""
        a, b = self.coef_x
        if not a:
            raise ValueError(f"{self.name}: column does not vary with stage position")
        w = (x - b) / a
        return w, self.coef_y[0] * w + self.coef_y[1]

    # -- the per-unit target ------------------------------------------------

    def set_target_stage(self, w):
        """Pin this unit at stage w; x and y follow from the fit."""
        w = float(w)
        x, y = self.xy_at(w)
        self.target_stage, self.target_x, self.target_y = w, x, y
        self.target_from = "stage"
        return w, x, y

    def set_target_x(self, x):
        """Pin this unit at column x; the stage and y follow from the fit."""
        w, y = self.stage_for_x(float(x))
        self.target_stage, self.target_x, self.target_y = w, float(x), y
        self.target_from = "x"
        return w, float(x), y

    def clear_target(self):
        self.target_stage = self.target_x = self.target_y = None
        self.target_from = ""

    @property
    def has_target(self):
        return self.target_stage is not None and self.target_x is not None

    # -- serialisation ------------------------------------------------------

    def to_dict(self):
        d = {
            "coef_x": list(self.coef_x), "coef_y": list(self.coef_y),
            "sigma_x": list(self.sigma_x), "sigma_y": list(self.sigma_y),
            "rms_px": None if self.rms_px != self.rms_px else self.rms_px,
            "n_frames": self.n_frames, "source": self.source,
            "updated_utc": self.updated_utc,
            "target_stage": self.target_stage,
            "target_x": self.target_x,
            "target_y": self.target_y,
            "target_from": self.target_from,
        }
        return d

    @classmethod
    def from_dict(cls, name, d):
        rms = d.get("rms_px")
        return cls(
            name=name,
            coef_x=tuple(d["coef_x"]), coef_y=tuple(d["coef_y"]),
            sigma_x=tuple(d.get("sigma_x", (0.0, 0.0))),
            sigma_y=tuple(d.get("sigma_y", (0.0, 0.0))),
            rms_px=float("nan") if rms is None else float(rms),
            n_frames=int(d.get("n_frames", 0)),
            source=d.get("source", ""), updated_utc=d.get("updated_utc", ""),
            target_stage=_opt_float(d.get("target_stage")),
            target_x=_opt_float(d.get("target_x")),
            target_y=_opt_float(d.get("target_y")),
            target_from=d.get("target_from", ""),
        )

    @classmethod
    def from_fit(cls, name, fit, source=""):
        """Build an entry from a TrackFit (core.linefit)."""
        import numpy as np

        return cls(
            name=name,
            coef_x=tuple(float(v) for v in fit.coef_x),
            coef_y=tuple(float(v) for v in fit.coef_y),
            sigma_x=tuple(float(v) for v in np.sqrt(np.maximum(np.diag(fit.cov_x), 0))),
            sigma_y=tuple(float(v) for v in np.sqrt(np.maximum(np.diag(fit.cov_y), 0))),
            rms_px=fit.rms(), n_frames=int(len(fit.positions)), source=source,
            updated_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


class UnitTable:
    """Unit name -> UnitEntry, persisted to a JSON file."""

    def __init__(self, path=None):
        self.path = None if path is None else str(path)
        self.units = {}

    def names(self):
        return sorted(self.units, key=_natural_key)

    def entries(self):
        return [self.units[n] for n in self.names()]

    def update(self, entry):
        """Insert or replace a unit; returns True if it replaced one."""
        replaced = entry.name in self.units
        self.units[entry.name] = entry
        return replaced

    def remove(self, name):
        self.units.pop(name, None)

    # -- interpolation over every unit -------------------------------------

    def at_stage(self, w):
        """[(name, x, y)] at stage position w."""
        return [(e.name, *e.xy_at(w)) for e in self.entries()]

    def at_x(self, x):
        """[(name, stage, y)] where each unit's column reaches x.

        Units whose column does not vary with stage are skipped.
        """
        out = []
        for e in self.entries():
            try:
                out.append((e.name, *e.stage_for_x(x)))
            except ValueError:
                continue
        return out

    # -- persistence --------------------------------------------------------

    def load(self, path=None):
        path = str(path or self.path)
        self.path = path
        self.units = {}
        if not os.path.exists(path):
            return self
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("schema") != SCHEMA:
            raise ValueError(f"{os.path.basename(path)} is not a unit table (schema {SCHEMA}).")
        for name, d in payload.get("units", {}).items():
            self.units[name] = UnitEntry.from_dict(name, d)
        return self

    def save(self, path=None):
        path = str(path or self.path)
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = {
            "schema": SCHEMA,
            "convention": "x is the image column, y is the image row; "
                          "col = coef_x[0]*w + coef_x[1], row = coef_y[0]*w + coef_y[1], w = stage position",
            "units": {n: self.units[n].to_dict() for n in self.names()},
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        return path


def _natural_key(name):
    import re

    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def guess_unit_name(directory):
    """'lab/mast03/1' -> 'mast03'; falls back to the directory's own name."""
    import re

    parts = os.path.normpath(directory).replace("\\", "/").split("/")
    for p in reversed(parts):
        if re.fullmatch(r"mast\d+", p, re.I):
            return p.lower()
    return parts[-1] if parts else ""
