"""The frame table: what was measured, what the operator overrode, what is in."""

import os
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont

from fifa_gui.core.linefit import fit_track

COL_INCLUDE, COL_POS, COL_REPEAT, COL_ROW, COL_COL, COL_RESID, COL_PEAK, COL_FLAGS = range(8)
HEADERS = ["", "Position", "Rep", "Row (y)", "Col (x)", "Resid (px)", "Peak", "Flags"]

FLAG_BRUSH = QBrush(QColor(90, 70, 20))
EXCLUDED_BRUSH = QBrush(QColor(60, 60, 60))


@dataclass
class FrameRecord:
    """One frame: its measurement, and the operator's judgement about it.

    The 93 MB array is deliberately not kept -- only a decimation pyramid and a
    small full-resolution cutout -- so a 63-frame dataset costs ~500 MB instead
    of 5.9 GB.
    """

    path: str
    position: int
    repeat: int = None

    row: float = float("nan")
    col: float = float("nan")
    peak: float = float("nan")
    flags: list = field(default_factory=list)
    message: str = ""

    roi_bounds: tuple = None
    bkg_bounds: tuple = None

    thumb: dict = None          # {decimation factor: array}
    cutout: np.ndarray = None
    cutout_origin: tuple = (0, 0)
    shape: tuple = (0, 0)

    include: bool = True
    override_row: float = None
    override_col: float = None
    residual: float = float("nan")

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def effective_row(self):
        return self.row if self.override_row is None else self.override_row

    @property
    def effective_col(self):
        return self.col if self.override_col is None else self.override_col

    @property
    def overridden(self):
        return self.override_row is not None or self.override_col is not None

    @property
    def all_flags(self):
        out = list(self.flags)
        if self.overridden:
            out.append("MANUAL")
        return out

    @property
    def usable(self):
        return (
            self.include
            and np.isfinite(self.effective_row)
            and np.isfinite(self.effective_col)
        )


class Dataset:
    """A directory of frames and the fit through them."""

    def __init__(self, directory):
        self.directory = directory
        self.records = []
        self.fit = None
        self.error = ""

    @property
    def name(self):
        parent = os.path.basename(os.path.dirname(self.directory))
        leaf = os.path.basename(self.directory)
        return f"{parent}/{leaf}" if parent else leaf

    def refit(self):
        """Re-fit from the currently included frames; cheap enough to be live."""
        self.fit = None
        self.error = ""
        used = [r for r in self.records if r.usable]
        for r in self.records:
            r.residual = float("nan")
        if len(used) < 2:
            self.error = f"Need at least 2 included frames, have {len(used)}."
            return None
        try:
            self.fit = fit_track(
                [r.position for r in used],
                [r.effective_row for r in used],
                [r.effective_col for r in used],
            )
        except ValueError as exc:
            self.error = str(exc)
            return None
        for rec, resid in zip(used, self.fit.residual_perp):
            rec.residual = float(resid)
        return self.fit


class FrameTableModel(QAbstractTableModel):
    #: a frame was included or excluded by the operator. Deliberately separate
    #: from dataChanged: the window re-fits on this, and a re-fit repaints the
    #: table, so debouncing off dataChanged would retrigger itself for ever.
    includeChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._records = []

    def set_records(self, records):
        self.beginResetModel()
        self._records = list(records)
        self.endResetModel()

    @property
    def records(self):
        return self._records

    def record_at(self, row):
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._records)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == COL_INCLUDE:
            return base | Qt.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        rec = self._records[index.row()]
        col = index.column()

        if role == Qt.CheckStateRole and col == COL_INCLUDE:
            return Qt.Checked if rec.include else Qt.Unchecked

        if role == Qt.DisplayRole:
            if col == COL_POS:
                return str(rec.position)
            if col == COL_REPEAT:
                return "" if rec.repeat is None else str(rec.repeat)
            if col == COL_ROW:
                return f"{rec.effective_row:.2f}" if np.isfinite(rec.effective_row) else "-"
            if col == COL_COL:
                return f"{rec.effective_col:.2f}" if np.isfinite(rec.effective_col) else "-"
            if col == COL_RESID:
                return f"{rec.residual:+.2f}" if np.isfinite(rec.residual) else "-"
            if col == COL_PEAK:
                return f"{rec.peak:.0f}" if np.isfinite(rec.peak) else "-"
            if col == COL_FLAGS:
                return " ".join(rec.all_flags)
            return None

        if role == Qt.ToolTipRole:
            bits = [rec.path]
            if rec.message:
                bits.append(rec.message)
            if rec.overridden:
                bits.append("Centroid set by hand.")
            return "\n".join(bits)

        if role == Qt.BackgroundRole:
            if not rec.include:
                return EXCLUDED_BRUSH
            if rec.all_flags:
                return FLAG_BRUSH

        if role == Qt.FontRole and not rec.include:
            font = QFont()
            font.setStrikeOut(True)
            return font

        if role == Qt.TextAlignmentRole and col >= COL_POS:
            return int(Qt.AlignRight | Qt.AlignVCenter)

        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid() or role != Qt.CheckStateRole or index.column() != COL_INCLUDE:
            return False
        self._records[index.row()].include = Qt.CheckState(value) == Qt.Checked
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        self.includeChanged.emit()
        return True

    def refresh(self):
        """Repaint every row after a re-fit changed the residuals."""
        if self._records:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._records) - 1, len(HEADERS) - 1),
            )
