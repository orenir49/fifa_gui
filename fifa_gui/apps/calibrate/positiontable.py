"""The editable list of stage positions a run will visit."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

COL_INDEX, COL_TARGET, COL_STATUS = range(3)
HEADERS = ["#", "Target", "Status"]

STATUS_COLORS = {
    "pending": QColor("#9e9e9e"),
    "current": QColor("#ffd54f"),
    "done": QColor("#66bb6a"),
    "failed": QColor("#ef5350"),
}


class PositionTableModel(QAbstractTableModel):
    def __init__(self, targets=None, parent=None):
        super().__init__(parent)
        self._targets = list(targets or [])
        self._status = ["pending"] * len(self._targets)
        self._limits = None

    # ------------------------------------------------------------- contents

    @property
    def targets(self):
        return list(self._targets)

    def set_targets(self, targets):
        self.beginResetModel()
        self._targets = [int(t) for t in targets]
        self._status = ["pending"] * len(self._targets)
        self.endResetModel()

    def set_limits(self, limits):
        self._limits = limits
        self.refresh()

    def set_status(self, index, status):
        if 0 <= index < len(self._status):
            self._status[index] = status
            self.dataChanged.emit(self.index(index, COL_STATUS),
                                  self.index(index, COL_STATUS))

    def reset_status(self):
        self._status = ["pending"] * len(self._targets)
        self.refresh()

    def refresh(self):
        if self._targets:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self._targets) - 1, COL_STATUS))

    # --------------------------------------------------------------- editing

    def add(self, value, after=None):
        position = len(self._targets) if after is None else after + 1
        self.beginInsertRows(QModelIndex(), position, position)
        self._targets.insert(position, int(value))
        self._status.insert(position, "pending")
        self.endInsertRows()
        return position

    def remove(self, row):
        if not (0 <= row < len(self._targets)):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._targets[row]
        del self._status[row]
        self.endRemoveRows()

    def move(self, row, delta):
        new = row + delta
        if not (0 <= row < len(self._targets) and 0 <= new < len(self._targets)):
            return row
        self.beginResetModel()
        self._targets[row], self._targets[new] = self._targets[new], self._targets[row]
        self.endResetModel()
        return new

    # ------------------------------------------------------------ Qt model

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._targets)

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
        if index.column() == COL_TARGET:
            return base | Qt.ItemIsEditable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()

        if role in (Qt.DisplayRole, Qt.EditRole):
            if col == COL_INDEX:
                return str(row + 1)
            if col == COL_TARGET:
                return self._targets[row] if role == Qt.EditRole else f"{self._targets[row]:,}"
            if col == COL_STATUS:
                return self._status[row]

        if role == Qt.ForegroundRole and col == COL_STATUS:
            return QBrush(STATUS_COLORS.get(self._status[row], QColor("#9e9e9e")))

        if role == Qt.BackgroundRole and self._limits and col == COL_TARGET:
            left, right = self._limits
            if not (left <= self._targets[row] <= right):
                return QBrush(QColor(120, 40, 40))

        if role == Qt.TextAlignmentRole and col == COL_TARGET:
            return int(Qt.AlignRight | Qt.AlignVCenter)

        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid() or role != Qt.EditRole or index.column() != COL_TARGET:
            return False
        try:
            self._targets[index.row()] = int(value)
        except (TypeError, ValueError):
            return False
        self.dataChanged.emit(index, index)
        return True

