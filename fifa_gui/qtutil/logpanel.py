"""A read-only log pane that can also be fed from a worker thread."""

import logging
from datetime import datetime

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QPlainTextEdit


class LogPanel(QPlainTextEdit):
    def __init__(self, parent=None, max_lines=5000):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(max_lines)
        self.setFont(QFont("Consolas", 9))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)

    def append_line(self, text, level=logging.INFO):
        stamp = datetime.now().strftime("%H:%M:%S")
        prefix = {logging.WARNING: "WARN ", logging.ERROR: "ERROR"}.get(level, "")
        self.appendPlainText(f"{stamp} {prefix}{text}".rstrip())
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


class QtLogBridge(QObject, logging.Handler):
    """A logging handler that re-emits records as a Qt signal.

    Records arriving from a worker thread reach the widget through a queued
    connection, which is the only thread-safe way to touch it.
    """

    record = Signal(int, str)

    def __init__(self):
        QObject.__init__(self)
        logging.Handler.__init__(self)

    def emit(self, record):
        try:
            self.record.emit(record.levelno, self.format(record))
        except Exception:
            self.handleError(record)
