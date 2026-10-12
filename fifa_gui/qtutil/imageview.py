"""A pan/zoom image view for 46-megapixel frames, with overlays.

Built on QGraphicsView rather than a matplotlib canvas: pan and zoom come free,
and mapToScene() hands back image coordinates directly, which is what
click-to-place a centroid needs. The pixmap is installed at a decimated level
and scaled back up by the same factor, so **scene coordinates are always
full-resolution image pixels** and overlays never have to be rescaled.
"""

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QImage, QPainter, QPen, QPixmap, QTransform,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsPixmapItem,
    QGraphicsRectItem, QGraphicsScene, QGraphicsView,
)

COLOR_CENTROID = QColor("#00e5ff")
COLOR_ROI = QColor("#ffd54f")
COLOR_BKG = QColor("#ff80ab")
COLOR_TRACK = QColor("#ff5252")
COLOR_OTHER = QColor("#ffffff")


def autoscale_levels(arr, lo_pct=1.0, hi_pct=99.5):
    """Display levels from percentiles of a decimated array."""
    lo, hi = np.percentile(arr, (lo_pct, hi_pct))
    if hi <= lo:
        hi = lo + 1.0
    return float(lo), float(hi)


def to_qimage(arr, lo, hi, log=False):
    """Map a 2-D array to an 8-bit grayscale QImage.

    With log=True the stretch is logarithmic, which is what makes a faint halo
    visible next to a near-saturated core -- the usual state of the fiber spot.

    QImage does not copy its buffer, so the backing array is kept alive on the
    returned object -- otherwise the view shows garbage or crashes.
    """
    data = arr.astype(np.float32)
    if log:
        span = max(hi - lo, 1e-9)
        norm = np.clip((data - lo) / span, 0.0, 1.0)
        # log1p over a decent dynamic range, normalised back to 0..1
        gain = 1000.0
        u8 = (np.log1p(norm * gain) / np.log1p(gain) * 255.0).astype(np.uint8)
    else:
        scale = 255.0 / max(hi - lo, 1e-9)
        u8 = np.clip((data - lo) * scale, 0, 255).astype(np.uint8)
    u8 = np.ascontiguousarray(u8)
    h, w = u8.shape
    img = QImage(u8.data, w, h, w, QImage.Format_Grayscale8)
    img._backing = u8
    return img


class ImageView(QGraphicsView):
    """Displays one frame plus overlay markers, in full-resolution coordinates."""

    #: emitted with (row, col) in full-frame coordinates on a left click
    clicked = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor("#1e1e1e")))
        self.setMinimumSize(320, 240)

        self._pixmap_item = None
        self._overlays = []
        self._shape = (0, 0)
        self._pick_mode = False

    # ------------------------------------------------------------------ frame

    def set_frame(self, arr, factor=1, levels=None, log=False):
        """Show `arr`, which is the full frame decimated by `factor`."""
        self._scene.clear()
        self._overlays = []
        self._pixmap_item = None
        if arr is None or arr.size == 0:
            self._shape = (0, 0)
            return

        lo, hi = levels if levels is not None else autoscale_levels(arr)
        pix = QPixmap.fromImage(to_qimage(arr, lo, hi, log=log))
        self._pixmap_item = QGraphicsPixmapItem(pix)
        # Scale the decimated pixmap back up, so scene units are full-res pixels.
        self._pixmap_item.setTransform(QTransform.fromScale(factor, factor))
        self._pixmap_item.setZValue(-100)
        self._scene.addItem(self._pixmap_item)

        self._shape = (arr.shape[0] * factor, arr.shape[1] * factor)
        self._scene.setSceneRect(QRectF(0, 0, self._shape[1], self._shape[0]))
        self.fit()

    def fit(self):
        if self._pixmap_item is not None:
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def zoom_to(self, row, col, half=120):
        """Centre the view on (row, col) at roughly 1:1."""
        self.fitInView(
            QRectF(col - half, row - half, 2 * half, 2 * half), Qt.KeepAspectRatio
        )

    # --------------------------------------------------------------- overlays

    def clear_overlays(self):
        for item in self._overlays:
            self._scene.removeItem(item)
        self._overlays = []

    def _add(self, item, color, width=0):
        pen = QPen(color)
        pen.setWidthF(width)
        pen.setCosmetic(True)       # constant on-screen width at any zoom
        item.setPen(pen)
        self._scene.addItem(item)
        self._overlays.append(item)
        return item

    def add_marker(self, row, col, color=COLOR_CENTROID, size=14):
        """A cross that keeps a constant screen size as the view zooms."""
        from PySide6.QtGui import QPainterPath

        path = QPainterPath()
        h = size / 2.0
        path.moveTo(-h, 0); path.lineTo(h, 0)
        path.moveTo(0, -h); path.lineTo(0, h)
        item = QGraphicsPathItem(path)
        item.setPos(QPointF(col, row))
        item.setFlag(QGraphicsPathItem.ItemIgnoresTransformations, True)
        return self._add(item, color, 1.5)

    def add_box(self, bounds, color=COLOR_ROI):
        r0, r1, c0, c1 = bounds
        return self._add(QGraphicsRectItem(c0, r0, c1 - c0, r1 - r0), color, 1.0)

    def add_circle(self, row, col, radius, color=COLOR_TRACK):
        return self._add(
            QGraphicsEllipseItem(col - radius, row - radius, 2 * radius, 2 * radius),
            color, 1.5,
        )

    def add_polyline(self, rows, cols, color=COLOR_TRACK):
        from PySide6.QtGui import QPainterPath

        if len(rows) < 2:
            return None
        path = QPainterPath(QPointF(cols[0], rows[0]))
        for r, c in zip(rows[1:], cols[1:]):
            path.lineTo(QPointF(c, r))
        item = QGraphicsPathItem(path)
        pen = QPen(color)
        pen.setWidthF(1.2)
        pen.setCosmetic(True)
        pen.setStyle(Qt.DashLine)
        item.setPen(pen)
        self._scene.addItem(item)
        self._overlays.append(item)
        return item

    # ------------------------------------------------------------ interaction

    def set_pick_mode(self, on):
        """When on, a left click emits `clicked` instead of panning."""
        self._pick_mode = bool(on)
        self.setDragMode(
            QGraphicsView.NoDrag if on else QGraphicsView.ScrollHandDrag
        )
        self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if self._pick_mode and event.button() == Qt.LeftButton:
            p = self.mapToScene(event.position().toPoint())
            if 0 <= p.y() < self._shape[0] and 0 <= p.x() < self._shape[1]:
                self.clicked.emit(float(p.y()), float(p.x()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._pixmap_item is None:
            return
        factor = 1.25 if event.angleDelta().y() > 0 else 1 / 1.25
        self.scale(factor, factor)
        event.accept()
