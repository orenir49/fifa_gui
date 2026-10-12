"""The Calculate window: load frames, check their centroids, fit, export."""

import logging
import os

import numpy as np
from PySide6.QtCore import QSettings, Qt, QThreadPool, QTimer, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QSpinBox, QSplitter, QTabWidget, QTableView, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
    QComboBox, QLineEdit, QTableWidget, QTableWidgetItem,
)

from fifa_gui import __version__
from fifa_gui.apps.calculate.loader import FrameLoadTask, LoaderSignals, read_window
from fifa_gui.apps.calculate.model import Dataset, FrameTableModel
from fifa_gui.core import export as core_export
from fifa_gui.core.centroid import find_centroid
from fifa_gui.core.flexure import DEFAULT_DIAMETER_UM, flexure_stats
from fifa_gui.core.imageio import scan_directory
from fifa_gui.core.unittable import UnitEntry, UnitTable, guess_unit_name
from fifa_gui.qtutil.imageview import (
    COLOR_BKG, COLOR_CENTROID, COLOR_OTHER, COLOR_ROI, COLOR_TRACK, ImageView,
)
from fifa_gui.qtutil.mplcanvas import MplPanel
from fifa_gui.paths import unit_table_path

logger = logging.getLogger(__name__)


class _StatusLog:
    """Stand-in for the log panel that used to sit under the tabs.

    The panel is gone; the messages are not. They go to the status bar,
    which costs no permanent space, and to the Python logger, which is
    what ends up in a file and is still there after the message clears.
    """

    def __init__(self, window):
        self._window = window

    def append_line(self, text, level=logging.INFO):
        logger.log(level, "%s", text)
        self._window.statusBar().showMessage(text, 10000)

#: the notebook's ZWO plate scale. The ASI294MM Pro reports 2.315 um pixels;
#: 2.35 is kept as the default so results match what has already been recorded.
#: Fixed reduction parameters, taken from the original calibration
#: notebook. They are not adjustable: these are the values the archived
#: lab/mast0X/1 datasets were reduced with, and with them mast03/1 gives
#: x/y(300000) = 4569.71/2812.83 and mast04/1 gives 4395.82/2536.49.
HALF_WIDTH = 4            # ROI half-size, int(5/2.35)*2 for a 5 um fiber
BG_OFFSET = 100           # rows away from the spot for the background box
CLIP_NEGATIVE = True      # a centre of mass with negative weights can
                          # land outside the data entirely
UM_PER_PX = 2.35          # the notebook's ZWO scale
STAGE_SPEC = 300000       # where the fiber position is reported
DEFAULT_X_TARGET = 4400
REFIT_DEBOUNCE_MS = 150


class CalculateWindow(QMainWindow):
    def __init__(self, unit_table_file=None):
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

        try:
            self.unit_table = UnitTable(unit_table_file or unit_table_path()).load()
        except (OSError, ValueError) as exc:
            self.unit_table = UnitTable(unit_table_file or unit_table_path())
            QMessageBox.warning(self, "Unit table", f"Could not read the unit table:\n{exc}")

        self._build_ui()
        self._restore_settings()
        self.log.append_line(
            f"FIFA Calculate {__version__}. Add a dataset directory to begin."
        )

    # ------------------------------------------------------------------- UI

    def _build_ui(self):
        self.log = _StatusLog(self)
        self._build_menu()

        self._build_inverse_widgets()
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_track_tab(), "Fitting")
        self.tabs.addTab(self._build_units_tab(), "Units")
        self.tabs.currentChanged.connect(lambda _: self._refresh_views())

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.tabs, 1)
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
        inspect_btn = QPushButton("Inspect frame...")
        inspect_btn.setToolTip(
            "Open the selected frame: ROI and background boxes, the centroid, "
            "and click-to-recentroid."
        )
        inspect_btn.clicked.connect(self._show_inspector)
        buttons = QHBoxLayout()
        buttons.addWidget(add_btn)
        buttons.addWidget(remove_btn)
        buttons.addWidget(reload_btn)
        buttons.addWidget(inspect_btn)

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

        # The frame itself is no longer on the Fitting tab, which is for the
        # two figures. It keeps its own window so click-to-recentroid and
        # the ROI overlays survive the move.
        self.inspector = QDialog(self)
        self.inspector.setWindowTitle("Frame inspector")
        self.inspector.setWindowFlag(Qt.Tool)
        self.inspector.resize(900, 720)
        inspector_layout = QVBoxLayout(self.inspector)
        inspector_layout.setContentsMargins(6, 6, 6, 6)
        inspector_layout.addWidget(self.view, 1)
        inspector_layout.addLayout(view_buttons)
        inspector_layout.addWidget(self.frame_label)

        # right: the two figures worth watching -- the flexure scatter and the
        # fit residuals -- over the parameter form. The track curve itself is
        # no longer shown; the panel is kept off-screen so Export still writes
        # track.png.
        self.track_plot = MplPanel(width=5, height=3.4)
        self.track_plot.setVisible(False)

        self.resid_plot = MplPanel(width=7, height=4.4)
        self.flex_plot = MplPanel(width=7, height=5.2)

        # Moved here with the plot: the acceptance circle is meaningless
        # without the diameter that draws it.
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

        flex_row = QHBoxLayout()
        flex_row.addWidget(QLabel("Acceptance circle diameter:"))
        flex_row.addWidget(self.flex_diameter)
        flex_row.addStretch(1)

        self.flex_label = QLabel(
            "Select a dataset with repeated frames at one position."
        )
        self.flex_label.setStyleSheet("font-family: Consolas, monospace;")
        self.flex_label.setWordWrap(True)

        flex_panel = QWidget()
        flex_layout = QVBoxLayout(flex_panel)
        flex_layout.setContentsMargins(0, 0, 0, 0)
        flex_layout.addLayout(flex_row)
        flex_layout.addWidget(self.flex_plot, 1)
        flex_layout.addWidget(self.flex_label)

        plots = QSplitter(Qt.Vertical)
        plots.addWidget(flex_panel)
        plots.addWidget(self.resid_plot)
        plots.setSizes([520, 440])

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(plots, 1)
        right_layout.addWidget(self._build_params_box())
        right_layout.addWidget(self._build_result_box())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([380, 1180])
        # The figures take every extra pixel; the dataset tree does not.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        right.setMinimumWidth(620)
        return splitter

    def _show_inspector(self):
        self.inspector.show()
        self.inspector.raise_()
        self.inspector.activateWindow()

    def _build_params_box(self):
        """The reduction parameters, shown but not editable.

        They are fixed in code at the notebook's values: every archived
        mast0X/1 dataset was reduced with these, so a stray edit would quietly
        make a new measurement incomparable with the ones already in the unit
        table. Change them in one place, with a reason, or not at all.
        """
        box = QGroupBox("Parameters (fixed)")
        form = QFormLayout(box)

        def fixed(text):
            label = QLabel(text)
            label.setStyleSheet("font-family: Consolas, monospace;")
            return label

        form.addRow("ROI half-width", fixed(f"{HALF_WIDTH} px"))
        form.addRow("Background offset", fixed(f"{BG_OFFSET} rows"))
        form.addRow("Clip negatives", fixed("yes" if CLIP_NEGATIVE else "no"))
        form.addRow("Pixel scale", fixed(f"{UM_PER_PX:g} um/px"))
        form.addRow("Stage spec", fixed(f"{STAGE_SPEC:,}"))
        return box

    def _build_result_box(self):
        box = QGroupBox(f"Fit at stage {STAGE_SPEC:,}")
        layout = QVBoxLayout(box)
        self.result_label = QLabel("No fit yet.")
        self.result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.result_label)

        export_btn = QPushButton("Export results...")
        export_btn.clicked.connect(self.export_results)
        layout.addWidget(export_btn)
        return box

    # --------------------------------------------------------- inverse tab

    def _build_inverse_widgets(self):
        """The Inverse tab is gone; the inverse solve behind it is not.

        Export still writes inverse.csv, so x_target survives as a stored
        parameter with no control of its own -- it keeps whatever the settings
        file holds. These two are parented to the window so Qt keeps them
        alive, and shown nowhere.
        """
        self.x_target = QDoubleSpinBox(self)
        self.x_target.setRange(-1e6, 1e6)
        self.x_target.setDecimals(2)
        self.x_target.setValue(DEFAULT_X_TARGET)
        self.x_target.setVisible(False)

        self.inverse_label = QLabel(self)
        self.inverse_label.setVisible(False)

    # ----------------------------------------------------------- units tab

    UNIT_HEADERS = ["Unit", "a_x", "b_x", "a_y", "b_y", "RMS (px)", "Frames",
                    "Updated (UTC)", "Source", "Stage", "x (col)", "y (row)"]

    def _build_units_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        top = QHBoxLayout()
        top.addWidget(QLabel("Unit:"))
        self.unit_name = QLineEdit()
        self.unit_name.setPlaceholderText("mast01")
        self.unit_name.setMaximumWidth(140)
        top.addWidget(self.unit_name)
        self.unit_update_btn = QPushButton("Update from selected dataset")
        self.unit_update_btn.setToolTip(
            "Store the selected dataset's fit coefficients for this unit,\n"
            "replacing the unit's previous entry, and save the table."
        )
        self.unit_update_btn.clicked.connect(self.update_unit_from_dataset)
        top.addWidget(self.unit_update_btn)
        self.unit_delete_btn = QPushButton("Delete unit")
        self.unit_delete_btn.clicked.connect(self.delete_unit)
        top.addWidget(self.unit_delete_btn)
        top.addStretch(1)
        layout.addLayout(top)

        interp = QHBoxLayout()
        interp.addWidget(QLabel("Interpolate:"))
        self.interp_mode = QComboBox()
        self.interp_mode.addItems(["stage + y from x", "x + y from stage"])
        self.interp_mode.currentIndexChanged.connect(self._on_interp_mode)
        interp.addWidget(self.interp_mode)
        self.interp_value = QDoubleSpinBox()
        self.interp_value.setRange(-1e9, 1e9)
        self.interp_value.setDecimals(2)
        self.interp_value.setValue(DEFAULT_X_TARGET)
        self.interp_value.valueChanged.connect(lambda _: self._refresh_units())
        interp.addWidget(self.interp_value)
        interp.addStretch(1)
        layout.addLayout(interp)

        self.units_table = QTableWidget(0, len(self.UNIT_HEADERS))
        self.units_table.setHorizontalHeaderLabels(self.UNIT_HEADERS)
        self.units_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.units_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.units_table.verticalHeader().setVisible(False)
        self.units_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.units_table.itemSelectionChanged.connect(self._on_unit_selected)
        layout.addWidget(self.units_table, 1)

        self.units_file_label = QLabel()
        self.units_file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.units_file_label)
        self._on_interp_mode()
        return widget

    def _on_interp_mode(self, *_):
        from_x = self.interp_mode.currentIndex() == 0
        self.interp_value.setValue(DEFAULT_X_TARGET if from_x else STAGE_SPEC)
        self.interp_value.setPrefix("x = " if from_x else "stage = ")
        self.interp_value.setDecimals(2 if from_x else 0)
        self._refresh_units()

    def _on_unit_selected(self):
        rows = self.units_table.selectionModel().selectedRows()
        if rows:
            self.unit_name.setText(self.units_table.item(rows[0].row(), 0).text())

    def update_unit_from_dataset(self):
        dataset = self._current_dataset()
        name = self.unit_name.text().strip()
        if dataset is None or dataset.fit is None:
            QMessageBox.information(self, "Unit table", "Select a dataset with a fit first.")
            return
        if not name:
            name = guess_unit_name(dataset.directory)
            self.unit_name.setText(name)
        entry = UnitEntry.from_fit(name, dataset.fit, source=dataset.directory)
        replaced = self.unit_table.update(entry)
        try:
            self.unit_table.save()
        except OSError as exc:
            QMessageBox.warning(self, "Unit table", f"Could not save the table:\n{exc}")
        self.log.append_line(
            f"{'Updated' if replaced else 'Added'} {name} in the unit table "
            f"(a_x={entry.coef_x[0]:.4e}, b_x={entry.coef_x[1]:.2f})."
        )
        self._refresh_units()

    def delete_unit(self):
        name = self.unit_name.text().strip()
        if name not in self.unit_table.units:
            return
        if QMessageBox.question(self, "Delete unit", f"Remove {name} from the table?") \
                != QMessageBox.Yes:
            return
        self.unit_table.remove(name)
        try:
            self.unit_table.save()
        except OSError as exc:
            QMessageBox.warning(self, "Unit table", f"Could not save the table:\n{exc}")
        self._refresh_units()

    def _refresh_units(self):
        table = self.units_table
        from_x = self.interp_mode.currentIndex() == 0
        value = self.interp_value.value()
        interp = {n: (a, b) for n, a, b in
                  (self.unit_table.at_x(value) if from_x else self.unit_table.at_stage(value))}

        entries = self.unit_table.entries()
        table.setRowCount(len(entries))
        for r, e in enumerate(entries):
            cells = [
                e.name, f"{e.coef_x[0]:.6e}", f"{e.coef_x[1]:.3f}",
                f"{e.coef_y[0]:.6e}", f"{e.coef_y[1]:.3f}",
                "" if e.rms_px != e.rms_px else f"{e.rms_px:.3f}",
                str(e.n_frames), e.updated_utc, e.source,
            ]
            if e.name in interp:
                a, b = interp[e.name]
                if from_x:      # a = stage, b = y
                    cells += [f"{a:.0f}", f"{value:.2f}", f"{b:.2f}"]
                else:           # a = x, b = y
                    cells += [f"{value:.0f}", f"{a:.2f}", f"{b:.2f}"]
            else:
                cells += ["n/a", "", ""]
            for c, text in enumerate(cells):
                table.setItem(r, c, QTableWidgetItem(text))
        self.units_file_label.setText(f"Table file: {self.unit_table.path}")

    # --------------------------------------------------------- flexure tab

    # ------------------------------------------------------------- settings

    def _params(self):
        return {
            "half_width": HALF_WIDTH,
            "bg_offset": BG_OFFSET,
            "clip_negative": CLIP_NEGATIVE,
            "um_per_px": UM_PER_PX,
            "stage_spec": STAGE_SPEC,
            "x_target": self.x_target.value(),
            "flex_diameter_um": self.flex_diameter.value(),
        }

    def _restore_settings(self):
        s = self.settings
        geometry = s.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        # The reduction parameters are fixed in code; nothing to restore.
        self.x_target.setValue(float(s.value("x_target", DEFAULT_X_TARGET)))
        self.flex_diameter.setValue(float(s.value("flex_diameter", DEFAULT_DIAMETER_UM)))

    def closeEvent(self, event):
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        for key, value in (
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
        self._refresh_units()
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
        spec = STAGE_SPEC
        scale = UM_PER_PX
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
                spec = STAGE_SPEC
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
        ax.axvline(STAGE_SPEC, color="r", ls="--", lw=1)

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
        scale = UM_PER_PX

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

        scale = UM_PER_PX
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

        half = max(HALF_WIDTH * 6, 64)
        r0, c0 = int(row) - half, int(col) - half
        try:
            window = read_window(record.path, r0, r0 + 2 * half, c0, c0 + 2 * half)
            local = find_centroid(
                window,
                half_width=HALF_WIDTH,
                bg_offset=min(BG_OFFSET, half),
                clip_negative=CLIP_NEGATIVE,
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
        # The reduction parameters are fixed in code now. A session saved
        # with different ones is still loaded, but it is re-reduced with
        # these -- so say so rather than silently changing the numbers.
        stale = {
            k: params[k] for k, fixed in (("half_width", HALF_WIDTH),
                                          ("bg_offset", BG_OFFSET),
                                          ("clip_negative", CLIP_NEGATIVE),
                                          ("um_per_px", UM_PER_PX),
                                          ("stage_spec", STAGE_SPEC))
            if k in params and params[k] != fixed
        }
        if stale:
            self.log.append_line(
                f"Session was saved with different parameters {stale}; using the fixed ones.",
                logging.WARNING,
            )
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
