"""Matplotlib canvases for the Calculate app's plots.

Explicit Figure objects, never pyplot's global manager: pyplot would try to run
its own event loop inside the Qt one. Used only for the scatter plots, whose
PNG export has to match the notebook's figures -- the frame viewer is an
ImageView instead.
"""

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import (
    NavigationToolbar2QT as NavigationToolbar,
)
from matplotlib.figure import Figure
from PySide6.QtWidgets import QVBoxLayout, QWidget


class MplCanvas(FigureCanvasQTAgg):
    def __init__(self, parent=None, width=5.0, height=4.0, dpi=100):
        self.figure = Figure(figsize=(width, height), dpi=dpi, layout="constrained")
        self.axes = self.figure.add_subplot(111)
        super().__init__(self.figure)
        self.setParent(parent)

    def clear(self):
        self.figure.clear()
        self.axes = self.figure.add_subplot(111)
        return self.axes

    def save_png(self, path, dpi=150):
        self.figure.savefig(path, dpi=dpi, facecolor=self.figure.get_facecolor())


class MplPanel(QWidget):
    """A canvas plus the standard matplotlib navigation toolbar."""

    def __init__(self, parent=None, **kwargs):
        super().__init__(parent)
        self.canvas = MplCanvas(self, **kwargs)
        self.toolbar = NavigationToolbar(self.canvas, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)

    @property
    def axes(self):
        return self.canvas.axes

    def clear(self):
        return self.canvas.clear()

    def draw(self):
        self.canvas.draw_idle()

    def save_png(self, path, dpi=150):
        self.canvas.save_png(path, dpi=dpi)
