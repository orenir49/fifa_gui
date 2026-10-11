"""The Calculate window: load frames, check their centroids, fit, export."""

import logging
import os

import numpy as np
from PySide6.QtCore import QSettings, Qt, QThreadPool, QTimer, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QSpinBox, QSplitter, QTabWidget, QTableView, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from fifa_gui import __version__
from fifa_gui.apps.calculate.loader import FrameLoadTask, LoaderSignals, read_window
from fifa_gui.apps.calculate.model import Dataset, FrameTableModel
from fifa_gui.core import export as core_export
from fifa_gui.core.centroid import find_centroid
from fifa_gui.core.flexure import DEFAULT_DIAMETER_UM, flexure_stats
from fifa_gui.core.imageio import scan_directory
from fifa_gui.qtutil.imageview import (
    COLOR_BKG, COLOR_CENTROID, COLOR_OTHER, COLOR_ROI, COLOR_TRACK, ImageView,
)
from fifa_gui.qtutil.logpanel import LogPanel
from fifa_gui.qtutil.mplcanvas import MplPanel

logger = logging.getLogger(__name__)

#: the notebook's ZWO plate scale. The ASI294MM Pro reports 2.315 um pixels;
#: 2.35 is kept as the default so results match what has already been recorded.
DEFAULT_UM_PER_PX = 2.35
DEFAULT_STAGE_SPEC = 300000
DEFAULT_X_TARGET = 4400
REFIT_DEBOUNCE_MS = 150


class CalculateWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"FIFA Calculate {__version__}")
        self.resize(1500, 950)

        self.settings = QSettings("WeizmannFIFA", "FIFA Calculate")
        self.datasets = []           # list[Dataset]
        self._pending = {}           # directory -> frames still loading
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(min(4, max(1, QThreadPool.globalInstance().maxThreadCount())))
        self._signals = LoaderSignals()
        self._signals.loaded.connect(self._on_frame_loaded)
        self._signals.failed.connect(self._on_frame_failed)

        self._refit_timer = QTimer(self)
        self._refit_timer.setSingleShot(True)
        self._refit_timer.setInterval(REFIT_DEBOUNCE_MS)
        self._refit_timer.timeout.connect(self._refit_all)

        self._build_ui()
        self._restore_settings()
        self.log.append_line(
            f"FIFA Calculate {__version__}. Add a dataset directory to begin."
        )

    # ------------------------------------------------------------------- UI

    def _build_ui(self):
        self._build_menu()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_track_tab(), "Track")
        self.tabs.addTab(self._build_inverse_tab(), "Inverse")
        self.tabs.addTab(self._build_flexure_tab(), "Flexure")
        self.tabs.currentChanged.connect(lambda _: self._refresh_views())

        self.log = LogPanel()
        self.log.setMaximumHeight(130)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.log)
        self.setCentralWidget(central)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(240)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("Ready")

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("&File")

        add = QAction("&Add dataset directory...", self)
        add.setShortcut(QKeySequence.Open)
        add.triggered.connect(self.add_dataset)
        file_menu.addAction(add)

        remove = QAction("&Remove selected dataset", self)
        remove.triggered.connect(self.remove_dataset)
        file_menu.addAction(remove)

        file_menu.addSeparator()
        save_session = QAction("&Save session...", self)
        save_session.triggered.connect(self.save_session)
        file_menu.addAction(save_session)

        open_session = QAction("&Open session...", self)
        open_session.triggered.connect(self.open_session)
        file_menu.addAction(open_session)

        file_menu.addSeparator()
        export = QAction("&Export results...", self)
        export.setShortcut("Ctrl+E")
        export.triggered.connect(self.export_results)
        file_menu.addAction(export)

        file_menu.addSeparator()
        quit_action = QAction("E&xit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("&About", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    # ---------------------------------------------------------- track tab

    def _build_track_tab(self):
        # left: dataset tree over the frame table
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Dataset", "Frames"])
        self.tree.setColumnWidth(0, 200)
        self.tree.currentItemChanged.connect(self._on_tree_selection)

        self.table = QTableView()
        self.table_model = FrameTableModel(self)
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 26)
        self.table.selectionModel().currentRowChanged.connect(self._on_frame_selected)
        # includeChanged, not dataChanged: a re-fit repaints the table via
        # refresh(), which emits dataChanged, which would restart this timer and
        # spin the fit and all three plots for ever.
        self.table_model.includeChanged.connect(self._refit_timer.start)

        add_btn = QPushButton("Add dataset...")
        add_btn.clicked.connect(self.add_dataset)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self.remove_dataset)
        reload_btn = QPushButton("Reload")
        reload_btn.setToolTip("Re-read the frames with the current ROI parameters")
        reload_btn.clicked.connect(self.reload_all)
        buttons = QHBoxLayout()
        buttons.addWidget(add_btn)
        buttons.addWidget(remove_btn)
        buttons.addWidget(reload_btn)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(buttons)
        left_layout.addWidget(self.tree, 1)
        left_layout.addWidget(QLabel("Frames (untick to exclude from the fit)"))
        left_layout.addWidget(self.table, 2)

        # middle: the inspector
        self.view = ImageView()
        self.view.clicked.connect(self._on_view_clicked)

        self.frame_label = QLabel("No frame selected")
        self.frame_label.setWordWrap(True)

        self.zoom_btn = QPushButton("Zoom to centroid")
        self.zoom_btn.clicked.connect(self._zoom_to_centroid)
        self.fit_view_btn = QPushButton("Fit view")
        self.fit_view_btn.clicked.connect(self.view.fit)
        self.pick_check = QCheckBox("Click to re-centroid")
        self.pick_check.setToolTip(
            "Click in the image to re-run the centroid from that point instead of\n"
            "the brightest pixel. Hold Shift to place the centroid exactly."
        )
        self.pick_check.toggled.connect(self.view.set_pick_mode)
        self.revert_btn = QPushButton("Revert to automatic")
        self.revert_btn.clicked.connect(self._revert_override)

        view_buttons = QHBoxLayout()
        view_buttons.addWidget(self.zoom_btn)
        view_buttons.addWidget(self.fit_view_btn)
        view_buttons.addWidget(self.pick_check)
        view_buttons.addWidget(self.revert_btn)
        view_buttons.addStretch(1)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.addWidget(self.view, 1)
        middle_layout.addLayout(view_buttons)
        middle_layout.addWidget(self.frame_label)

        # right: plots over the parameter form
        self.track_plot = MplPanel(width=5, height=3.4)
        self.resid_plot = MplPanel(width=5, height=3.0)

        plots = QSplitter(Qt.Vertical)
        plots.addWidget(self.track_plot)
        plots.addWidget(self.resid_plot)
        plots.setSizes([340, 300])

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(plots, 1)
        right_layout.addWidget(self._build_params_box())
        right_layout.addWidget(self._build_result_box())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(middle)
        splitter.addWidget(right)
        splitter.setSizes([380, 540, 520])
        return splitter

    def _build_params_box(self):
        box = QGroupBox("Parameters")
        form = QFormLayout(box)

        self.half_width = QSpinBox()
        self.half_width.setRange(1, 200)
        self.half_width.setValue(4)
        self.half_width.setToolTip(
            "ROI half-size in pixels. The notebook used 4 (from int(5/2.35)*2 for\n"
            "a 5 um fiber). Changing this needs the frames re-read: press Reload."
        )

        self.bg_offset = QSpinBox()
        self.bg_offset.setRange(1, 2000)
        self.bg_offset.setValue(100)
        self.bg_offset.setToolTip("Rows away from the spot for the background box.")

        self.clip_check = QCheckBox("Clip negatives")
        self.clip_check.setChecked(True)
        self.clip_check.setToolTip(
            "A centre of mass with negative weights can land outside the data."
        )

        self.um_per_px = QDoubleSpinBox()
        self.um_per_px.setRange(0.01, 100.0)
        self.um_per_px.setDecimals(3)
        self.um_per_px.setValue(DEFAULT_UM_PER_PX)
        self.um_per_px.setSuffix(" um/px")
        self.um_per_px.setToolTip(
            "The notebook used 2.35 for the ZWO; the ASI294MM Pro reports 2.315."
        )
        self.um_per_px.valueChanged.connect(lambda _: self._refresh_views())

        self.stage_spec = QSpinBox()
        self.stage_spec.setRange(-10_000_000, 10_000_000)
        self.stage_spec.setValue(DEFAULT_STAGE_SPEC)
        self.stage_spec.setToolTip("Stage position at which to report the fiber position.")
        self.stage_spec.valueChanged.connect(lambda _: self._refresh_views())

        form.addRow("ROI half-width", self.half_width)
        form.addRow("Background offset", self.bg_offset)
        form.addRow("", self.clip_check)
        form.addRow("Pixel scale", self.um_per_px)
        form.addRow("Stage spec", self.stage_spec)
        return box

    def _build_result_box(self):
        box = QGroupBox("Fit at the stage spec")
        layout = QVBoxLayout(box)
        self.result_label = QLabel("No fit yet.")
        self.result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.result_label)

        export_btn = QPushButton("Export results...")
        export_btn.clicked.connect(self.export_results)
        layout.addWidget(export_btn)
        return box

    # --------------------------------------------------------- inverse tab

    def _build_inverse_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        form = QHBoxLayout()
        form.addWidget(QLabel("Target column x:"))
        self.x_target = QDoubleSpinBox()
        self.x_target.setRange(-1e6, 1e6)
        self.x_target.setDecimals(2)
        self.x_target.setValue(DEFAULT_X_TARGET)
        self.x_target.valueChanged.connect(lambda _: self._refresh_inverse())
        form.addWidget(self.x_target)
        form.addStretch(1)
        layout.addLayout(form)

        self.inverse_label = QLabel(
            "Solves col(w) = x for the stage position w, per dataset, and averages them."
        )
        self.inverse_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.inverse_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.inverse_label.setStyleSheet("font-family: Consolas, monospace;")
        layout.addWidget(self.inverse_label, 1)
        return widget

    # --------------------------------------------------------- flexure tab

    def _build_flexure_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        form = QHBoxLayout()
        form.addWidget(QLabel("Acceptance circle diameter:"))
        self.flex_diameter = QDoubleSpinBox()
        self.flex_diameter.setRange(0.1, 10000.0)
        self.flex_diameter.setDecimals(1)
        self.flex_diameter.setValue(DEFAULT_DIAMETER_UM)
        self.flex_diameter.setSuffix(" um")
        self.flex_diameter.setToolTip(
            "Diameter, not radius. The notebook drew plt.Circle(centre, 30/2.35),\n"
            "whose second argument is the radius -- so it plotted a 60 um circle\n"
            "while labelling it 30 um. Enter 60 to reproduce the old plot."
        )
        self.flex_diameter.valueChanged.connect(lambda _: self._refresh_flexure())
        form.addWidget(self.flex_diameter)
        form.addStretch(1)
        layout.addLayout(form)

        self.flex_plot = MplPanel(width=6, height=6)
        layout.addWidget(self.flex_plot, 1)

        self.flex_label = QLabel("Select a dataset with repeated frames at one position.")
        self.flex_label.setStyleSheet("font-family: Consolas, monospace;")
        layout.addWidget(self.flex_label)
        return widget

    # ------------------------------------------------------------- settings

    def _params(self):
        return {
            "half_width": self.half_width.value(),
            "bg_offset": self.bg_offset.value(),
            "clip_negative": self.clip_check.isChecked(),
            "um_per_px": self.um_per_px.value(),
            "stage_spec": self.stage_spec.value(),
            "x_target": self.x_target.value(),
            "flex_diameter_um": self.flex_diameter.value(),
        }

    def _restore_settings(self):
        s = self.settings
        geometry = s.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self.half_width.setValue(int(s.value("half_width", 4)))
        self.bg_offset.setValue(int(s.value("bg_offset", 100)))
        self.clip_check.setChecked(s.value("clip_negative", True, type=bool))
        self.um_per_px.setValue(float(s.value("um_per_px", DEFAULT_UM_PER_PX)))
        self.stage_spec.setValue(int(s.value("stage_spec", DEFAULT_STAGE_SPEC)))
        self.x_target.setValue(float(s.value("x_target", DEFAULT_X_TARGET)))
        self.flex_diameter.setValue(float(s.value("flex_diameter", DEFAULT_DIAMETER_UM)))

    def closeEvent(self, event):
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        for key, value in (
            ("half_width", self.half_width.value()),
            ("bg_offset", self.bg_offset.value()),
            ("clip_negative", self.clip_check.isChecked()),
            ("um_per_px", self.um_per_px.value()),
            ("stage_spec", self.stage_spec.value()),
            ("x_target", self.x_target.value()),
            ("flex_diameter", self.flex_diameter.value()),
        ):
            s.setValue(key, value)
        self._pool.waitForDone(3000)
        event.accept()

    # -------------------------------------------------------------- loading

    def add_dataset(self):
        start = self.settings.value("last_dir", "", type=str)
        directory = QFileDialog.getExistingDirectory(self, "Add dataset directory", start)
        if directory:
            self.settings.setValue("last_dir", directory)
            self.load_directory(directory)

    def load_directory(self, directory):
        directory = os.path.normpath(directory)
        if any(d.directory == directory for d in self.datasets):
            self.log.append_line(f"{directory} is already loaded.", logging.WARNING)
            return

        try:
            frames = scan_directory(directory)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot read directory", str(exc))
            return
        if not frames:
            QMessageBox.information(
                self, "No frames",
                f"No frames named after a stage position were found in\n{directory}",
            )
            return

        dataset = Dataset(directory)
        self.datasets.append(dataset)
        self._pending[directory] = len(frames)
        self._update_tree()

        params = self._params()
        for path, position, repeat in frames:
            self._pool.start(
                FrameLoadTask(directory, path, position, repeat, params, self._signals)
            )
        self.log.append_line(f"Loading {len(frames)} frames from {directory} ...")
        self._update_progress()

    def reload_all(self):
        """Re-read every dataset: the ROI parameters need the full frames again."""
        if not self.datasets:
            return
        directories = [d.directory for d in self.datasets]
        keep = {
            d.directory: {
                r.name: (r.include, r.override_row, r.override_col) for r in d.records
            }
            for d in self.datasets
        }
        self.datasets = []
        self._update_tree()
        for directory in directories:
            self.load_directory(directory)
        self._restore_judgements = keep

    @Slot(str, object)
    def _on_frame_loaded(self, directory, record):
        # Decrement first and unconditionally: if the dataset was removed while
        # its frames were still in flight, an early return here would leave the
        # pending count stuck above zero for ever.
        self._pending[directory] = max(0, self._pending.get(directory, 1) - 1)
        dataset = self._dataset(directory)
        if dataset is None:
            self._update_progress()
            return
        # Re-apply the operator's judgement after a Reload.
        keep = getattr(self, "_restore_judgements", {}).get(directory, {})
        if record.name in keep:
            record.include, record.override_row, record.override_col = keep[record.name]
        dataset.records.append(record)
        if self._pending[directory] == 0:
            dataset.records.sort(key=lambda r: (r.position, -1 if r.repeat is None else r.repeat))
            flagged = sum(1 for r in dataset.records if r.all_flags)
            self.log.append_line(
                f"Loaded {len(dataset.records)} frames from {dataset.name}"
                + (f" ({flagged} flagged)" if flagged else "")
            )
            self._refit_timer.start()
            self._update_tree()
        self._update_progress()

    @Slot(str, str, str)
    def _on_frame_failed(self, directory, path, message):
        self._pending[directory] = max(0, self._pending.get(directory, 1) - 1)
        self.log.append_line(f"{os.path.basename(path)}: {message}", logging.ERROR)
        self._update_progress()

    def _update_progress(self):
        remaining = sum(self._pending.values())
        if remaining:
            self.progress.setVisible(True)
            self.progress.setRange(0, 0)
            self.statusBar().showMessage(f"Loading {remaining} frames ...")
        else:
            self.progress.setVisible(False)
            self.statusBar().showMessage("Ready")

    def remove_dataset(self):
        dataset = self._current_dataset()
        if dataset is None:
            return
        self.datasets.remove(dataset)
        self._pending.pop(dataset.directory, None)
        self.log.append_line(f"Removed {dataset.name}.")
        self._update_tree()
        self._refit_all()

    # --------------------------------------------------------------- fitting

    def _refit_all(self):
        for dataset in self.datasets:
            dataset.refit()
        self.table_model.refresh()
        self._refresh_views()

    def _refresh_views(self):
        self._draw_track()
        self._draw_residuals()
        self._update_result_label()
        self._refresh_inverse()
        self._refresh_flexure()

    def _update_result_label(self):
        dataset = self._current_dataset()
        if dataset is None:
            self.result_label.setText("No dataset selected.")
            return
        if dataset.fit is None:
            self.result_label.setText(f"{dataset.name}: {dataset.error}")
            return

        fit = dataset.fit
        spec = self.stage_spec.value()
        scale = self.um_per_px.value()
        self.result_label.setText(
            f"<b>{dataset.name}</b> at stage {spec}<br>"
            f"x (column) = {fit.x_at(spec):.2f} &plusmn; {fit.sigma_x_at(spec):.2f} px<br>"
            f"y (row)&nbsp;&nbsp;&nbsp; = {fit.y_at(spec):.2f} &plusmn; {fit.sigma_y_at(spec):.2f} px<br>"
            f"residual rms {fit.rms():.2f} px ({fit.rms() * scale:.2f} um), "
            f"max {np.max(np.abs(fit.residual_perp)):.2f} px<br>"
            f"{sum(1 for r in dataset.records if r.usable)} of "
            f"{len(dataset.records)} frames included"
        )

    def _draw_track(self):
        ax = self.track_plot.clear()
        ax.set_xlabel("x = column (px)")
        ax.set_ylabel("y = row (px)")
        ax.set_title("Fiber track")

        any_data = False
        for dataset in self.datasets:
            used = [r for r in dataset.records if r.usable]
            if not used:
                continue
            any_data = True
            cols = [r.effective_col for r in used]
            rows = [r.effective_row for r in used]
            line = ax.scatter(cols, rows, s=18, label=dataset.name)
            if dataset.fit is not None:
                w = np.linspace(min(dataset.fit.positions), max(dataset.fit.positions), 50)
                ax.plot(
                    np.polyval(dataset.fit.coef_x, w),
                    np.polyval(dataset.fit.coef_y, w),
                    ls="--", lw=1, color=line.get_facecolor()[0],
                )
                spec = self.stage_spec.value()
                ax.plot(
                    dataset.fit.x_at(spec), dataset.fit.y_at(spec),
                    marker="x", ms=10, mew=2, ls="none",
                    color=line.get_facecolor()[0],
                )
        if any_data:
            ax.legend(fontsize=8)
            ax.invert_yaxis()   # row 0 is the top of the image
        else:
            ax.text(0.5, 0.5, "No frames loaded", ha="center", va="center",
                    transform=ax.transAxes, color="grey")
        self.track_plot.draw()

    def _draw_residuals(self):
        ax = self.resid_plot.clear()
        ax.set_xlabel("stage position")
        ax.set_ylabel("residual (px)")
        ax.set_title("Perpendicular residual")
        ax.axhline(0, color="k", ls="--", lw=1)
        ax.axvline(self.stage_spec.value(), color="r", ls="--", lw=1)

        any_data = False
        for dataset in self.datasets:
            if dataset.fit is None:
                continue
            used = [r for r in dataset.records if r.usable]
            ax.scatter(
                [r.position for r in used],
                [r.residual for r in used],
                s=20, label=dataset.name,
            )
            any_data = True
        if any_data:
            ax.legend(fontsize=8)
        self.resid_plot.draw()

    def _refresh_inverse(self):
        x_target = self.x_target.value()
        lines = [f"{'dataset':<20}{'a_x':>14}{'b_x':>12}{'position':>12}{'y (row)':>10}", "-" * 68]
        positions, ys = [], []
        for dataset in self.datasets:
            if dataset.fit is None:
                lines.append(f"{dataset.name:<20}{'(no fit)':>48}")
                continue
            try:
                pos, y = dataset.fit.position_for_x(x_target)
            except ValueError as exc:
                lines.append(f"{dataset.name:<20}{str(exc):>48}")
                continue
            positions.append(pos)
            ys.append(y)
            lines.append(
                f"{dataset.name:<20}{dataset.fit.coef_x[0]:>14.3e}"
                f"{dataset.fit.coef_x[1]:>12.1f}{pos:>12.0f}{y:>10.1f}"
            )
        if positions:
            lines.append("-" * 68)
            lines.append(f"{'mean':<20}{'':>26}{np.mean(positions):>12.0f}{np.mean(ys):>10.1f}")
            lines.append(f"{'std':<20}{'':>26}{np.std(positions):>12.0f}{np.std(ys):>10.1f}")
        self.inverse_label.setText("\n".join(lines))

    def _refresh_flexure(self):
        dataset = self._current_dataset()
        ax = self.flex_plot.clear()
        scale = self.um_per_px.value()

        if dataset is None or not dataset.records:
            ax.text(0.5, 0.5, "No dataset selected", ha="center", va="center",
                    transform=ax.transAxes, color="grey")
            self.flex_plot.draw()
            self.flex_label.setText("Select a dataset with repeated frames at one position.")
            return

        used = [r for r in dataset.records if r.usable]
        if len(used) < 2:
            ax.text(0.5, 0.5, "Need at least 2 frames", ha="center", va="center",
                    transform=ax.transAxes, color="grey")
            self.flex_plot.draw()
            return

        stats = flexure_stats(
            [r.effective_row for r in used],
            [r.effective_col for r in used],
            um_per_px=scale,
            diameter_um=self.flex_diameter.value(),
        )
        ax.scatter(stats.cols, stats.rows, s=24, c="tab:red", label="centroids")
        ax.scatter([stats.mean_col], [stats.mean_row], s=110, marker="x",
                   c="tab:blue", label="mean")
        circle = __import__("matplotlib.patches", fromlist=["Circle"]).Circle(
            (stats.mean_col, stats.mean_row), stats.radius_px,
            fill=False, color="tab:green", lw=2,
            label=f"{stats.diameter_um:.0f} um diameter",
        )
        ax.add_patch(circle)
        margin = max(stats.radius_px * 1.6, stats.max_px * 1.4, 2.0)
        ax.set_xlim(stats.mean_col - margin, stats.mean_col + margin)
        ax.set_ylim(stats.mean_row - margin, stats.mean_row + margin)
        ax.set_aspect("equal")
        ax.set_xlabel("x = column (px)")
        ax.set_ylabel("y = row (px)")
        ax.set_title(f"Flexure: {dataset.name}")
        ax.legend(fontsize=8)
        self.flex_plot.draw()

        verdict = "PASS" if stats.passed else "FAIL"
        self.flex_label.setText(
            f"n = {len(used)}   rms {stats.rms_px:.2f} px ({stats.rms_um:.2f} um)   "
            f"max {stats.max_px:.2f} px ({stats.max_um:.2f} um)   "
            f"circle radius {stats.radius_px:.2f} px   ->  {verdict}"
        )

    # ------------------------------------------------------------- selection

    def _dataset(self, directory):
        for d in self.datasets:
            if d.directory == directory:
                return d
        return None

    def _current_dataset(self):
        item = self.tree.currentItem()
        if item is None:
            return self.datasets[0] if self.datasets else None
        while item.parent() is not None:
            item = item.parent()
        return self._dataset(item.data(0, Qt.UserRole))

    def _update_tree(self):
        current = self._current_dataset()
        self.tree.blockSignals(True)
        self.tree.clear()
        target_item = None
        for dataset in self.datasets:
            item = QTreeWidgetItem([dataset.name, str(len(dataset.records))])
            item.setData(0, Qt.UserRole, dataset.directory)
            item.setToolTip(0, dataset.directory)
            self.tree.addTopLevelItem(item)
            if current is not None and dataset.directory == current.directory:
                target_item = item
        self.tree.blockSignals(False)
        if target_item is not None:
            self.tree.setCurrentItem(target_item)
        elif self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self.table_model.set_records([])
            self.view.set_frame(None)

    def _on_tree_selection(self, *_):
        dataset = self._current_dataset()
        self.table_model.set_records(dataset.records if dataset else [])
        if dataset and dataset.records:
            self.table.selectRow(0)
        self._refresh_views()

    def _on_frame_selected(self, current, _previous):
        record = self.table_model.record_at(current.row()) if current.isValid() else None
        self._show_frame(record)

    def _show_frame(self, record):
        self._current_record = record
        if record is None or record.thumb is None:
            self.view.set_frame(None)
            self.frame_label.setText("No frame selected")
            return

        factor = max(record.thumb)
        self.view.set_frame(record.thumb[factor], factor=factor)
        self._draw_overlays(record)

        scale = self.um_per_px.value()
        resid = (f"{record.residual:+.2f} px ({record.residual * scale:+.2f} um)"
                 if np.isfinite(record.residual) else "-")
        flags = " ".join(record.all_flags) or "none"
        self.frame_label.setText(
            f"<b>{record.name}</b> &nbsp; stage {record.position}"
            f"{'' if record.repeat is None else f' rep {record.repeat}'}<br>"
            f"centroid row {record.effective_row:.2f}, col {record.effective_col:.2f} "
            f"&nbsp; residual {resid}<br>"
            f"peak {record.peak:.0f} ADU &nbsp; flags: {flags}"
        )

    def _draw_overlays(self, record):
        self.view.clear_overlays()
        if record.roi_bounds:
            self.view.add_box(record.roi_bounds, COLOR_ROI)
        if record.bkg_bounds:
            self.view.add_box(record.bkg_bounds, COLOR_BKG)
        if np.isfinite(record.effective_row):
            self.view.add_marker(record.effective_row, record.effective_col, COLOR_CENTROID)

        dataset = self._current_dataset()
        if dataset is not None:
            others = [r for r in dataset.records if r is not record and r.usable]
            for other in others:
                self.view.add_marker(other.effective_row, other.effective_col,
                                     COLOR_OTHER, size=6)
            if dataset.fit is not None:
                w = np.array([min(dataset.fit.positions), max(dataset.fit.positions)])
                self.view.add_polyline(
                    np.polyval(dataset.fit.coef_y, w),
                    np.polyval(dataset.fit.coef_x, w),
                    COLOR_TRACK,
                )

    def _zoom_to_centroid(self):
        record = getattr(self, "_current_record", None)
        if record is not None and np.isfinite(record.effective_row):
            self.view.zoom_to(record.effective_row, record.effective_col, half=60)

    # ------------------------------------------------------- manual override

    def _on_view_clicked(self, row, col):
        """Re-run the centroid seeded at the click, reading only that window.

        np.argmax over a 46 MP frame is fragile: a cosmic ray or a reflection
        steals it. This is the fix for that, and it never loads the full frame.
        """
        record = getattr(self, "_current_record", None)
        if record is None:
            return

        half = max(self.half_width.value() * 6, 64)
        r0, c0 = int(row) - half, int(col) - half
        try:
            window = read_window(record.path, r0, r0 + 2 * half, c0, c0 + 2 * half)
            local = find_centroid(
                window,
                half_width=self.half_width.value(),
                bg_offset=min(self.bg_offset.value(), half),
                clip_negative=self.clip_check.isChecked(),
                seed=(row - max(r0, 0), col - max(c0, 0)),
            )
        except Exception as exc:
            self.log.append_line(f"Re-centroid failed: {exc}", logging.ERROR)
            return

        if not local.ok:
            self.log.append_line(f"Re-centroid failed: {local.message}", logging.WARNING)
            return

        record.override_row = local.row + max(r0, 0)
        record.override_col = local.col + max(c0, 0)
        record.roi_bounds = tuple(
            b + (max(r0, 0) if i < 2 else max(c0, 0))
            for i, b in enumerate(local.roi_bounds)
        )
        record.bkg_bounds = tuple(
            b + (max(r0, 0) if i < 2 else max(c0, 0))
            for i, b in enumerate(local.bkg_bounds)
        )
        self.log.append_line(
            f"{record.name}: centroid set by hand to "
            f"row {record.override_row:.2f}, col {record.override_col:.2f}"
        )
        self._refit_all()
        self._show_frame(record)
        self.table_model.refresh()

    def _revert_override(self):
        record = getattr(self, "_current_record", None)
        if record is None or not record.overridden:
            return
        record.override_row = None
        record.override_col = None
        self.log.append_line(f"{record.name}: reverted to the automatic centroid.")
        self._refit_all()
        self._show_frame(record)
        self.table_model.refresh()

    # ---------------------------------------------------------------- export

    def export_results(self):
        if not self.datasets:
            QMessageBox.information(self, "Nothing to export", "Load a dataset first.")
            return
        start = self.settings.value("last_export", "", type=str)
        directory = QFileDialog.getExistingDirectory(self, "Export results into", start)
        if not directory:
            return
        self.settings.setValue("last_export", directory)

        params = self._params()
        written = []
        try:
            for dataset in self.datasets:
                stem = dataset.name.replace("/", "_").replace("\\", "_")
                written.append(core_export.write_frames_csv(
                    os.path.join(directory, f"{stem}_frames.csv"),
                    dataset.records, dataset.fit,
                ))
            written.append(core_export.write_fit_json(
                os.path.join(directory, "fits.json"), self.datasets, params, __version__))
            written.append(core_export.write_inverse_csv(
                os.path.join(directory, "inverse.csv"), self.datasets, params["x_target"]))
            written.append(core_export.write_session(
                os.path.join(directory, "session.json"), self.datasets, params, __version__))

            self.track_plot.save_png(os.path.join(directory, "track.png"))
            self.resid_plot.save_png(os.path.join(directory, "residuals.png"))
            self.flex_plot.save_png(os.path.join(directory, "flexure.png"))
            written += [os.path.join(directory, n)
                        for n in ("track.png", "residuals.png", "flexure.png")]
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return

        self.log.append_line(f"Exported {len(written)} files to {directory}")
        QMessageBox.information(
            self, "Export complete",
            f"Wrote {len(written)} files into\n{directory}",
        )

    def save_session(self):
        if not self.datasets:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save session", "session.json", "JSON (*.json)")
        if not path:
            return
        core_export.write_session(path, self.datasets, self._params(), __version__)
        self.log.append_line(f"Session saved to {path}")

    def open_session(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open session", "", "JSON (*.json)")
        if not path:
            return
        try:
            payload = core_export.read_session(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot open session", str(exc))
            return

        params = payload.get("parameters", {})
        self.half_width.setValue(int(params.get("half_width", 4)))
        self.bg_offset.setValue(int(params.get("bg_offset", 100)))
        self.clip_check.setChecked(bool(params.get("clip_negative", True)))
        self.um_per_px.setValue(float(params.get("um_per_px", DEFAULT_UM_PER_PX)))
        self.stage_spec.setValue(int(params.get("stage_spec", DEFAULT_STAGE_SPEC)))
        self.x_target.setValue(float(params.get("x_target", DEFAULT_X_TARGET)))
        self.flex_diameter.setValue(float(params.get("flex_diameter_um", DEFAULT_DIAMETER_UM)))

        self.datasets = []
        self._update_tree()
        self._restore_judgements = {}
        missing = []
        for entry in payload.get("datasets", []):
            directory = entry["directory"]
            if not os.path.isdir(directory):
                missing.append(directory)
                continue
            self._restore_judgements[os.path.normpath(directory)] = {
                f["file"]: (f.get("include", True),
                            f.get("override_row"), f.get("override_col"))
                for f in entry.get("frames", [])
            }
            self.load_directory(directory)

        self.log.append_line(f"Session restored from {path}")
        if missing:
            QMessageBox.warning(
                self, "Some directories are missing",
                "These directories from the session no longer exist:\n\n"
                + "\n".join(missing),
            )

    def _about(self):
        QMessageBox.about(
            self, "About FIFA Calculate",
            f"<b>FIFA Calculate {__version__}</b><br><br>"
            "Centroids fiber frames, fits the track against stage position and "
            "evaluates it at a target stage value.<br><br>"
            "Reporting convention: <b>x is the image column, y is the image row</b>."
        )
