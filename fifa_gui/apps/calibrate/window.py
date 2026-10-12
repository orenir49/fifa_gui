"""The Calibrate window: drive the stage and camera through a position sweep."""

import json
import logging
import os

import numpy as np
from PySide6.QtCore import QSettings, Qt, QThread, Slot
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider,
    QSpinBox, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from fifa_gui import __version__
from fifa_gui.apps.calibrate import commands
from fifa_gui.apps.calibrate.positiontable import PositionTableModel
from fifa_gui.apps.calibrate.worker import (
    STATE_ABORTED, STATE_DONE, STATE_FAILED, STATE_IDLE, STATE_RUNNING,
    STATE_WAITING_OPERATOR, AcquisitionWorker,
)
from fifa_gui.core.sequence import (
    DEFAULT_BACKOFF, DEFAULT_EXPOSURE_US, DEFAULT_GAIN, DEFAULT_OFFSET,
    DEFAULT_REPEATS, DEFAULT_SETTLE_MS, CameraSettings, SequencePlan, validate,
)
from fifa_gui.qtutil.imageview import COLOR_CENTROID, ImageView
from fifa_gui.qtutil.logpanel import LogPanel

logger = logging.getLogger(__name__)

DEFAULT_TARGETS = [285000, 295000, 305000, 315000, 325000, 330000]

BANNER_STYLE = (
    "QFrame { background: #7f1d1d; border: 1px solid #b91c1c; border-radius: 4px; }"
    "QLabel { color: #fee2e2; font-weight: bold; }"
)
PAUSE_STYLE = (
    "QFrame { background: #7f1d1d; border: 3px solid #ef4444; border-radius: 6px; }"
    "QLabel { color: #fecaca; font-weight: bold; font-size: 20pt; }"
)

#: The operator is at the bench, not the screen, so the pause has to shout.
CONTINUE_WAITING_STYLE = (
    "QPushButton { background: #dc2626; color: white; font-size: 20pt;"
    " font-weight: bold; border: 3px solid #fca5a5; border-radius: 6px; }"
    "QPushButton:hover { background: #ef4444; }"
)
CONTINUE_IDLE_STYLE = ""

#: Shown while the sequence is paused, in the order the steps are done: the
#: filter comes out to see the spot and goes back before the frame is taken,
#: because forgetting to put it back is what saturates the frame.
PAUSE_PROMPT = "REMOVE ND FILTER, CENTER FIBER, RETURN ND FILTER, PRESS CONTINUE"

#: Green when the device is open, red when it is not. With the panels behind
#: buttons, these carry the hardware state as colour instead of as text.
HARDWARE_OK_STYLE = (
    "QPushButton { background: #166534; color: #dcfce7; font-weight: bold;"
    " border: 1px solid #22c55e; border-radius: 4px; padding: 4px 10px; }"
    "QPushButton:hover { background: #15803d; }"
)
HARDWARE_BAD_STYLE = (
    "QPushButton { background: #7f1d1d; color: #fecaca; font-weight: bold;"
    " border: 1px solid #ef4444; border-radius: 4px; padding: 4px 10px; }"
    "QPushButton:disabled { background: #7f1d1d; color: #fecaca; }"
)

#: There is no default output directory on purpose: the operator must name the
#: unit every run, or a sweep silently lands in the previous unit's folder.
OUT_DIR_PROMPT = 'Choose output directory with the correct unit name, i.e. "./data/mast0x"'
OUT_DIR_PROMPT_STYLE = "color: #ef4444; font-weight: bold;"


class CalibrateWindow(QMainWindow):
    def __init__(self, auto_connect=True):
        super().__init__()
        self.setWindowTitle(f"FIFA Calibrate {__version__}")
        self.resize(1400, 900)

        self.settings = QSettings("WeizmannFIFA", "FIFA Calibrate")
        self._limits = None
        self._state = STATE_IDLE
        self._camera_info = None
        self._stage_info = None
        self._problems = []

        self._build_ui()
        self._restore_settings()
        self._start_worker()

        self.log.append_line(f"FIFA Calibrate {__version__}")
        if auto_connect:
            # The probe runs on the worker thread, so the window is already
            # visible by the time enumeration finishes -- it can take seconds.
            self._reconnect()
        else:
            # --no-connect: open without touching the devices, so the app can be
            # started while someone else is using the bench.
            self.banner_label.setText(
                "Started with --no-connect: no devices opened. Press Reconnect "
                "when the bench is free."
            )
            self.banner.setVisible(True)
            self.log.append_line("--no-connect: the camera and stage were not opened.")

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        self._build_menu()

        self.banner = QFrame()
        self.banner.setStyleSheet(BANNER_STYLE)
        self.banner_label = QLabel("Looking for hardware ...")
        self.banner_label.setWordWrap(True)
        reconnect = QPushButton("Reconnect")
        reconnect.clicked.connect(self._reconnect)
        details = QPushButton("Details")
        details.clicked.connect(self._show_problems)
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.setContentsMargins(10, 6, 10, 6)
        banner_layout.addWidget(self.banner_label, 1)
        banner_layout.addWidget(details)
        banner_layout.addWidget(reconnect)

        # The stage and camera panels are behind the compact row at the top,
        # in windows of their own: the left column belongs to the sweep,
        # which is what an operator actually drives during a run.
        left = QWidget()
        left.setFixedWidth(430)
        panel_layout = QVBoxLayout(left)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.addLayout(self._build_hardware_row())
        panel_layout.addWidget(self._build_sequence_box(), 1)

        right = QSplitter(Qt.Vertical)
        right.addWidget(self._build_preview_box())
        right.addWidget(self._build_progress_box())
        right.setSizes([560, 280])

        body = QHBoxLayout()
        body.addWidget(left)
        body.addWidget(right, 1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.banner)
        layout.addWidget(self._build_pause_banner())
        layout.addLayout(body, 1)
        self.setCentralWidget(central)

        self.status_label = QLabel("Idle")
        self.statusBar().addPermanentWidget(self.status_label)

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("&File")
        load = QAction("&Load plan...", self)
        load.triggered.connect(self.load_plan)
        file_menu.addAction(load)
        save = QAction("&Save plan...", self)
        save.triggered.connect(self.save_plan)
        file_menu.addAction(save)
        file_menu.addSeparator()
        quit_action = QAction("E&xit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        hw_menu = self.menuBar().addMenu("&Hardware")
        reconnect = QAction("&Reconnect", self)
        reconnect.triggered.connect(self._reconnect)
        hw_menu.addAction(reconnect)
        # The toolbar buttons go dead when a device is not connected, so these
        # stay live: without them a manual stage URI could not be typed.
        stage_panel = QAction("&Stage panel...", self)
        stage_panel.triggered.connect(lambda: self._show_popup(self.stage_dialog))
        hw_menu.addAction(stage_panel)
        camera_panel = QAction("&Camera panel...", self)
        camera_panel.triggered.connect(lambda: self._show_popup(self.camera_dialog))
        hw_menu.addAction(camera_panel)
        locate = QAction("&Locate ASICamera2.dll...", self)
        locate.triggered.connect(self.locate_dll)
        hw_menu.addAction(locate)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("&About", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _build_pause_banner(self):
        self.pause_banner = QFrame()
        self.pause_banner.setStyleSheet(PAUSE_STYLE)
        self.pause_label = QLabel()
        self.pause_label.setWordWrap(True)
        layout = QHBoxLayout(self.pause_banner)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addWidget(self.pause_label, 1)
        self.pause_banner.setVisible(False)
        return self.pause_banner

    def _build_hardware_row(self):
        """Compact entry points to the stage and camera, plus the live readouts.

        Both panels are only touched at the start of a session -- pick a URI,
        set gain and exposure -- so they do not earn permanent space, but the
        position and the sensor temperature are watched throughout and stay on
        the main window. Test exposure stays here too: it is used between
        positions, as part of running the sweep.
        """
        self.position_label = QLabel("position: -")
        self.position_label.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 13pt;"
        )
        self.temp_label = QLabel("sensor: -")
        self.temp_label.setStyleSheet("color: grey;")

        self.test_btn = QPushButton("Test exposure")
        self.test_btn.setToolTip("Take one frame and show it, without saving")
        self.test_btn.clicked.connect(
            lambda: self.worker.post(commands.TestExposure(self._camera_settings()))
        )

        self.stage_dialog = self._popup("Stage", self._build_stage_box())
        self.camera_dialog = self._popup("Camera", self._build_camera_box())

        self.stage_btn = QPushButton("Stage")
        self.stage_btn.clicked.connect(lambda: self._show_popup(self.stage_dialog))
        self.camera_btn = QPushButton("Camera")
        self.camera_btn.clicked.connect(lambda: self._show_popup(self.camera_dialog))

        buttons = QHBoxLayout()
        buttons.addWidget(self.stage_btn)
        buttons.addWidget(self.camera_btn)
        buttons.addWidget(self.test_btn, 1)

        layout = QVBoxLayout()
        layout.addLayout(buttons)
        layout.addWidget(self.position_label)
        layout.addWidget(self.temp_label)
        return layout

    def _popup(self, title, inner):
        """Put a control box in a window of its own.

        Modeless, so the stage can be jogged while the preview is watched, and
        Qt.Tool so it floats above the main window instead of hiding behind it.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setWindowFlag(Qt.Tool)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(inner)
        return dialog

    def _show_popup(self, dialog):
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _build_stage_box(self):
        box = QGroupBox("Stage")
        layout = QVBoxLayout(box)

        self.stage_uri = QComboBox()
        self.stage_uri.setEditable(True)
        self.stage_uri.lineEdit().setPlaceholderText(r"auto-detect, or xi-com:\\.\COM5")

        self.limits_label = QLabel("limits: -")

        self.home_btn = QPushButton("Home")
        self.home_btn.setToolTip("command_homezero: drive to the home switch and zero there")
        self.home_btn.clicked.connect(lambda: self.worker.post(commands.HomeZero()))
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._abort)

        self.goto_spin = QSpinBox()
        self.goto_spin.setRange(-10_000_000, 10_000_000)
        self.goto_spin.setGroupSeparatorShown(True)
        self.goto_btn = QPushButton("Go")
        self.goto_btn.clicked.connect(
            lambda: self.worker.post(commands.MoveTo(self.goto_spin.value()))
        )

        form = QFormLayout()
        form.addRow("URI", self.stage_uri)
        layout.addLayout(form)
        layout.addWidget(self.limits_label)

        buttons = QHBoxLayout()
        buttons.addWidget(self.home_btn)
        buttons.addWidget(self.stop_btn)
        layout.addLayout(buttons)

        jog = QHBoxLayout()
        jog.addWidget(QLabel("Go to"))
        jog.addWidget(self.goto_spin, 1)
        jog.addWidget(self.goto_btn)
        layout.addLayout(jog)
        return box

    def _build_camera_box(self):
        box = QGroupBox("Camera")
        layout = QVBoxLayout(box)

        self.camera_label = QLabel("not connected")
        self.camera_label.setWordWrap(True)

        self.gain = QSpinBox()
        self.gain.setRange(0, 600)
        self.gain.setValue(DEFAULT_GAIN)
        self.gain.setToolTip(
            "Unity gain by default: the fiber spot is bright enough that "
            "gain only adds read noise."
        )

        self.offset = QSpinBox()
        # Wide enough for the real default; narrowed to the camera's own
        # range on connect.
        self.offset.setRange(0, 100_000)
        self.offset.setGroupSeparatorShown(True)
        self.offset.setValue(DEFAULT_OFFSET)
        self.offset.setSuffix(" ADU")
        self.offset.setToolTip(
            "ASI_OFFSET: the black level. Lifts the floor off zero so the "
            "background noise is symmetric rather than clipped at 0."
        )

        # Microseconds, which is the unit ASI_EXPOSURE takes. The useful end for
        # a bright fiber spot is the short one -- around 32 us -- so a spin box
        # that bottomed out at 1 ms could not reach a usable exposure at all.
        # The real range is read from the camera on connect.
        self.exposure = QSpinBox()
        self.exposure.setRange(1, 2_000_000_000)
        self.exposure.setValue(DEFAULT_EXPOSURE_US)
        self.exposure.setSuffix(" us")
        self.exposure.setGroupSeparatorShown(True)
        self.exposure.setToolTip(
            "Exposure in microseconds. The notebook never set this at all, so\n"
            "frames ran at whatever the SDK default was -- which is how the\n"
            "archived datasets came out saturated."
        )
        self.exposure.valueChanged.connect(self._update_exposure_hint)
        self.exposure_hint = QLabel()
        self.exposure_hint.setStyleSheet("color: grey;")
        self.image_type = QComboBox()
        self.image_type.addItems(["RAW16", "RAW8"])

        self.cooler = QCheckBox("Cooler on")
        self.target_temp = QSpinBox()
        self.target_temp.setRange(-40, 30)
        self.target_temp.setValue(-10)
        self.target_temp.setSuffix(" C")

        form = QFormLayout()
        form.addRow("Gain", self.gain)
        form.addRow("Offset (bias)", self.offset)
        form.addRow("Exposure", self.exposure)
        form.addRow("", self.exposure_hint)
        form.addRow("Image type", self.image_type)
        form.addRow("", self.cooler)
        form.addRow("Target temp", self.target_temp)

        self.apply_btn = QPushButton("Apply")
        self.apply_btn.clicked.connect(
            lambda: self.worker.post(commands.ApplyCamera(self._camera_settings()))
        )
        layout.addWidget(self.camera_label)
        layout.addLayout(form)
        layout.addWidget(self.apply_btn)
        return box

    def _build_sequence_box(self):
        box = QGroupBox("Sequence")
        layout = QVBoxLayout(box)

        self.out_dir = QLineEdit()
        self.out_dir.setPlaceholderText("./data/mast0x")
        self.out_dir.textChanged.connect(lambda _: self._revalidate())
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._choose_out_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.out_dir, 1)
        dir_row.addWidget(browse)

        self.out_dir_prompt = QLabel(OUT_DIR_PROMPT)
        self.out_dir_prompt.setStyleSheet(OUT_DIR_PROMPT_STYLE)
        self.out_dir_prompt.setWordWrap(True)

        # Back-off, settle, repeats and the operator pause are fixed: they are
        # properties of the bench, not of a run, and getting one of them wrong
        # silently costs a whole sweep.
        fixed = QLabel(
            f"Back-off {DEFAULT_BACKOFF:,} | settle {DEFAULT_SETTLE_MS} ms | "
            f"{DEFAULT_REPEATS} frame per position | pauses at every position"
        )
        fixed.setStyleSheet("color: grey;")
        fixed.setWordWrap(True)

        self.overwrite_check = QCheckBox("Allow overwriting existing frames")
        self.overwrite_check.toggled.connect(lambda _: self._revalidate())

        self.position_model = PositionTableModel(DEFAULT_TARGETS, self)
        self.position_model.dataChanged.connect(lambda *_: self._revalidate())
        self.position_model.rowsInserted.connect(lambda *_: self._revalidate())
        self.position_model.rowsRemoved.connect(lambda *_: self._revalidate())
        self.position_view = QTableView()
        self.position_view.setModel(self.position_model)
        self.position_view.verticalHeader().setVisible(False)
        self.position_view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.position_view.setMinimumHeight(220)
        # Without SelectRows, clicking a cell selects only that cell and
        # selectedRows() comes back empty -- which made +/-/^/v silently do
        # nothing. ExtendedSelection lets several rows be removed at once.
        self.position_view.setSelectionBehavior(QTableView.SelectRows)
        self.position_view.setSelectionMode(QTableView.ExtendedSelection)
        self.position_view.setEditTriggers(
            QTableView.DoubleClicked | QTableView.SelectedClicked | QTableView.EditKeyPressed
        )

        add = QPushButton("+")
        add.setFixedWidth(30)
        add.setToolTip("Add a position below the selected row (Insert)")
        add.clicked.connect(self._add_position)
        remove = QPushButton("-")
        remove.setFixedWidth(30)
        remove.setToolTip("Remove the selected rows, or the last one (Delete)")
        remove.clicked.connect(self._remove_position)
        up = QPushButton("^"); up.setFixedWidth(30); up.clicked.connect(lambda: self._move_position(-1))
        down = QPushButton("v"); down.setFixedWidth(30); down.clicked.connect(lambda: self._move_position(1))
        clear = QPushButton("Clear"); clear.clicked.connect(self._clear_positions)
        table_buttons = QHBoxLayout()
        for widget in (add, remove, up, down, clear):
            table_buttons.addWidget(widget)
        table_buttons.addStretch(1)

        QShortcut(QKeySequence(Qt.Key_Delete), self.position_view,
                  activated=self._remove_position)
        QShortcut(QKeySequence(Qt.Key_Insert), self.position_view,
                  activated=self._add_position)

        self.count_label = QLabel()
        self.count_label.setStyleSheet("color: grey;")

        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)

        self.start_btn = QPushButton("START")
        self.start_btn.setMinimumHeight(38)
        self.start_btn.clicked.connect(self._start)
        self.continue_btn = QPushButton("CONTINUE")
        self.continue_btn.setMinimumHeight(38)
        self.continue_btn.clicked.connect(self._continue)
        self.abort_btn = QPushButton("ABORT")
        self.abort_btn.setMinimumHeight(38)
        self.abort_btn.clicked.connect(self._abort)
        run_buttons = QHBoxLayout()
        run_buttons.addWidget(self.start_btn)
        run_buttons.addWidget(self.continue_btn)
        run_buttons.addWidget(self.abort_btn)

        layout.addWidget(QLabel("Output directory"))
        layout.addLayout(dir_row)
        layout.addWidget(self.out_dir_prompt)
        layout.addWidget(fixed)
        layout.addWidget(self.overwrite_check)
        self.sweep_limits_label = QLabel("Stage limits: not connected")
        self.sweep_limits_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(QLabel("Positions"))
        layout.addWidget(self.sweep_limits_label)
        layout.addWidget(self.position_view, 1)
        layout.addLayout(table_buttons)
        layout.addWidget(self.count_label)
        layout.addWidget(self.validation_label)
        layout.addLayout(run_buttons)
        return box

    def _build_preview_box(self):
        box = QGroupBox("Last frame")
        layout = QVBoxLayout(box)
        self.view = ImageView()
        self.frame_label = QLabel("No frame yet. Use 'Test exposure' or start a sequence.")
        self.frame_label.setWordWrap(True)

        self._last_preview = None     # kept so rescaling needs no new exposure
        self._last_meta = None

        self.auto_levels = QCheckBox("Auto")
        self.auto_levels.setChecked(True)
        self.auto_levels.setToolTip(
            "Scale from the background up to the brightest pixel.\n"
            "Untick to set vmin and vmax by hand."
        )
        self.auto_levels.toggled.connect(self._on_auto_levels_toggled)

        # Sliders, not boxes: a cut on a 16-bit frame is dragged until it
        # looks right, not a number anyone knows up front. The readouts
        # beside them keep the actual value legible.
        self.vmin = QSlider(Qt.Horizontal)
        self.vmin.setRange(0, 65535)
        self.vmin.setEnabled(False)
        self.vmin.valueChanged.connect(self._redraw_last_frame)
        self.vmin_label = QLabel("0")
        self.vmin_label.setMinimumWidth(52)
        self.vmin_label.setStyleSheet("font-family: Consolas, monospace;")

        self.vmax = QSlider(Qt.Horizontal)
        self.vmax.setRange(1, 65535)
        self.vmax.setValue(65535)
        self.vmax.setEnabled(False)
        self.vmax.valueChanged.connect(self._redraw_last_frame)
        self.vmax_label = QLabel("65,535")
        self.vmax_label.setMinimumWidth(52)
        self.vmax_label.setStyleSheet("font-family: Consolas, monospace;")
        self.log_levels = QCheckBox("Log")
        self.log_levels.setChecked(False)
        self.log_levels.setToolTip(
            "Logarithmic stretch, so the faint halo is visible next to a core\n"
            "that is near full well."
        )
        self.log_levels.toggled.connect(self._redraw_last_frame)

        levels = QHBoxLayout()
        levels.addWidget(QLabel("Scale:"))
        levels.addWidget(self.auto_levels)
        levels.addWidget(QLabel("vmin"))
        levels.addWidget(self.vmin, 1)
        levels.addWidget(self.vmin_label)
        levels.addWidget(QLabel("vmax"))
        levels.addWidget(self.vmax, 1)
        levels.addWidget(self.vmax_label)
        levels.addWidget(self.log_levels)

        layout.addWidget(self.view, 1)
        layout.addLayout(levels)
        layout.addWidget(self.frame_label)
        return box

    def _on_auto_levels_toggled(self, on):
        self.vmin.setEnabled(not on)
        self.vmax.setEnabled(not on)
        self._redraw_last_frame()

    def _auto_levels_for(self, arr):
        """Background floor up to the brightest pixel.

        Not a high percentile: the spot covers a handful of pixels out of 46
        million, so even the 99.5th percentile is still background and the
        whole frame clips to white.
        """
        lo = float(np.percentile(arr, 50.0))
        hi = float(arr.max())
        if hi <= lo:
            hi = lo + 1.0
        return lo, hi

    def _redraw_last_frame(self, *_):
        if self._last_preview is None:
            return
        if self.auto_levels.isChecked():
            lo, hi = self._auto_levels_for(self._last_preview)
            self.vmin.blockSignals(True); self.vmax.blockSignals(True)
            self.vmin.setValue(int(lo))
            self.vmax.setValue(max(int(hi), 1))
            self.vmin.blockSignals(False); self.vmax.blockSignals(False)
        else:
            lo, hi = float(self.vmin.value()), float(self.vmax.value())
            if hi <= lo:
                hi = lo + 1.0
        self.vmin_label.setText(f"{int(lo):,}")
        self.vmax_label.setText(f"{int(hi):,}")
        self.view.set_frame(
            self._last_preview, factor=8, levels=(lo, hi),
            log=self.log_levels.isChecked(),
        )
        meta = self._last_meta
        if meta and np.isfinite(meta.get("peak", float("nan"))):
            try:
                row, col = np.unravel_index(
                    int(np.argmax(self._last_preview)), self._last_preview.shape
                )
                self.view.add_marker(row * 8, col * 8, COLOR_CENTROID)
            except Exception:
                pass

    def _build_progress_box(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m")
        self.phase_label = QLabel("Idle")
        row = QHBoxLayout()
        row.addWidget(self.phase_label, 1)
        row.addWidget(self.progress, 1)

        self.log = LogPanel()
        layout.addLayout(row)
        layout.addWidget(self.log, 1)
        return widget

    # -------------------------------------------------------------- worker

    def _start_worker(self):
        self.thread = QThread(self)
        self.thread.setObjectName("hardware")
        self.worker = AcquisitionWorker()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)

        self.worker.hardware_changed.connect(self._on_hardware_changed)
        self.worker.exposure_range.connect(self._on_exposure_range)
        self.worker.offset_range.connect(self._on_offset_range)
        self.worker.position_changed.connect(self._on_position)
        self.worker.limits_changed.connect(self._on_limits)
        self.worker.temperature.connect(self._on_temperature)
        self.worker.state_changed.connect(self._on_state)
        self.worker.frame_progress.connect(self._on_progress)
        self.worker.frame_ready.connect(self._on_frame)
        self.worker.frame_saved.connect(self._on_saved)
        self.worker.log.connect(self.log.append_line)
        self.worker.sequence_finished.connect(self._on_finished)

        self.thread.start()

    def closeEvent(self, event):
        self._save_settings()
        self.worker.request_abort()
        self.worker.operator_continue()      # release a pause so the loop can unwind
        self.worker.post(commands.Shutdown())
        self.thread.quit()
        if not self.thread.wait(10_000):
            logger.warning("hardware thread did not stop; terminating")
            self.thread.terminate()
            self.thread.wait(2000)
        event.accept()

    # --------------------------------------------------------- worker slots

    @Slot(object, object, list)
    def _on_hardware_changed(self, camera_info, stage_info, problems):
        self._camera_info = camera_info
        self._stage_info = stage_info
        self._problems = problems

        if camera_info is None:
            self.camera_label.setText("not connected")
        else:
            self.camera_label.setText(f"{camera_info.name}\n{camera_info.detail}")
        if stage_info is not None:
            self.stage_uri.setEditText(stage_info.uri)

        missing = []
        if camera_info is None:
            missing.append("camera")
        if stage_info is None:
            missing.append("stage")
        if missing:
            what = " and ".join(missing)
            self.banner_label.setText(
                f"Unable to connect to the {what}. Make sure ASIStudio / XILab / "
                f"a Python or Jupyter kernel is not using the {what}, then press "
                f"Reconnect.  (Details for more.)"
            )
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)
        self._update_enabled()
        self._revalidate()

    def _update_exposure_hint(self, *_):
        us = self.exposure.value()
        if us >= 1_000_000:
            self.exposure_hint.setText(f"= {us / 1e6:.3f} s")
        else:
            self.exposure_hint.setText(f"= {us / 1000.0:.3f} ms")

    @Slot(object)
    def _on_exposure_range(self, rng):
        """Clamp the spin box to what this camera actually accepts."""
        if not rng:
            self._update_exposure_hint()
            return
        lo, hi, default = rng
        self.exposure.setRange(int(lo), int(hi))
        self.exposure.setToolTip(
            f"Exposure in microseconds.\n"
            f"This camera accepts {lo:,} .. {hi:,} us (default {default:,})."
        )
        if self.exposure.value() < lo:
            self.exposure.setValue(int(lo))
        self.exposure_hint.setText(
            f"= {self.exposure.value() / 1000.0:.3f} ms   (camera min {lo:,} us)"
        )

    @Slot(object)
    def _on_offset_range(self, rng):
        """Clamp the offset box to what this camera actually accepts."""
        if not rng:
            return
        lo, hi, default = rng
        self.offset.setRange(int(lo), int(hi))
        self.offset.setToolTip(
            f"ASI_OFFSET, the black level, in ADU.\n"
            f"This camera accepts {lo:,} .. {hi:,} (default {default:,})."
        )

    @Slot(int)
    def _on_position(self, position):
        self.position_label.setText(f"position: {position:,}")

    @Slot(int, int)
    def _on_limits(self, left, right):
        self._limits = (left, right)
        self.limits_label.setText(f"limits: {left:,} .. {right:,}")
        self._update_sweep_limits()
        self.goto_spin.setRange(left, right)
        self.position_model.set_limits(self._limits)
        self._revalidate()

    @Slot(object, object)
    def _on_temperature(self, sensor_c, cooler_pct):
        if sensor_c is None:
            self.temp_label.setText("sensor: -")
        else:
            self.temp_label.setText(f"sensor: {sensor_c:+.1f} C   cooler {cooler_pct:.0f}%")

    @Slot(str, dict)
    def _on_state(self, state, payload):
        self._state = state
        self.status_label.setText(state.replace("_", " ").title())

        if state == STATE_WAITING_OPERATOR:
            position = payload.get("position")
            self.pause_label.setText(f"{PAUSE_PROMPT}\n(stage at {position:,})")
            self.pause_banner.setVisible(True)
            self.continue_btn.setStyleSheet(CONTINUE_WAITING_STYLE)
            self.continue_btn.setMinimumHeight(80)
            self.continue_btn.setText("CONTINUE")
            self.continue_btn.setDefault(True)
            # Bring the window to the front: the operator is at the bench and
            # will not see a banner on a window that is behind everything else.
            self.setWindowState(
                (self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive
            )
            self.raise_()
            self.activateWindow()
            self.continue_btn.setFocus()
            QApplication.beep()
            QApplication.alert(self)
        else:
            self.pause_banner.setVisible(False)
            self.continue_btn.setStyleSheet(CONTINUE_IDLE_STYLE)
            self.continue_btn.setMinimumHeight(38)
            self.continue_btn.setDefault(False)

        if state in (STATE_DONE, STATE_ABORTED, STATE_FAILED):
            self.position_model.reset_status()
            if payload.get("detail"):
                self.phase_label.setText(payload["detail"])
        self._update_enabled()

    @Slot(int, int, str)
    def _on_progress(self, index, total, phase):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(index)
        self.phase_label.setText(f"Frame {max(index, 1)} / {total}: {phase}")

    @Slot(object, dict)
    def _on_frame(self, preview, meta):
        self._last_preview = preview
        self._last_meta = meta
        self._redraw_last_frame()
        saturated = "  SATURATED" if meta["saturated"] else ""
        position = meta.get("position")
        where = f"stage {position:,}   " if position is not None else ""
        self.frame_label.setText(
            f"{where}peak {meta['peak']:.0f} ADU "
            f"({meta['peak_native']:.0f}/4095){saturated}   "
            f"{meta['shape'][1]} x {meta['shape'][0]}"
        )

    @Slot(str, int)
    def _on_saved(self, path, size):
        self.log.append_line(f"saved {os.path.basename(path)} ({size / 1e6:.1f} MB)")

    @Slot(bool, str)
    def _on_finished(self, ok, message):
        self.log.append_line(message, logging.INFO if ok else logging.WARNING)
        self.phase_label.setText(message)
        if ok:
            QMessageBox.information(self, "Sequence complete", message)

    # ----------------------------------------------------------- UI actions

    def _camera_settings(self):
        return CameraSettings(
            gain=self.gain.value(),
            offset=self.offset.value(),
            exposure_us=self.exposure.value(),
            image_type=self.image_type.currentText(),
            cooler_on=self.cooler.isChecked(),
            target_temp_c=self.target_temp.value(),
        )

    def _plan(self):
        return SequencePlan(
            targets=self.position_model.targets,
            out_dir=self.out_dir.text().strip(),
            backoff=DEFAULT_BACKOFF,
            settle_ms=DEFAULT_SETTLE_MS,
            repeats=DEFAULT_REPEATS,
            overwrite=self.overwrite_check.isChecked(),
            pause_for_operator=True,
            camera=self._camera_settings(),
        )

    def _update_sweep_limits(self):
        if not self._limits:
            self.sweep_limits_label.setText("Stage limits: not connected")
            return
        left, right = self._limits
        usable_low = left + DEFAULT_BACKOFF
        self.sweep_limits_label.setText(
            f"Stage limits: {left:,} .. {right:,}   |   "
            f"usable here: {usable_low:,} .. {right:,} "
            f"(back-off {DEFAULT_BACKOFF:,})"
        )

    def _revalidate(self):
        self._update_sweep_limits()
        plan = self._plan()
        n = len(plan.targets)
        self.count_label.setText(
            f"{n} position{'' if n == 1 else 's'}, {plan.frame_count} frame"
            f"{'' if plan.frame_count == 1 else 's'}"
        )
        problems = validate(plan, self._limits)
        errors = [p.text for p in problems if p.level == "error"]
        warnings = [p.text for p in problems if p.level == "warning"]

        if errors:
            self.validation_label.setText(
                "<span style='color:#f87171'>" + "<br>".join(errors) + "</span>"
            )
        elif warnings:
            self.validation_label.setText(
                "<span style='color:#fbbf24'>" + "<br>".join(warnings) + "</span>"
            )
        else:
            self.validation_label.setText("")
        self._blocking_errors = bool(errors)
        self._update_enabled()

    def _update_enabled(self):
        has_stage = self._stage_info is not None
        has_camera = self._camera_info is not None
        running = self._state in (STATE_RUNNING, STATE_WAITING_OPERATOR)
        can_start = (
            has_stage and has_camera and not running
            and not getattr(self, "_blocking_errors", True)
        )

        self.start_btn.setEnabled(can_start)
        self.continue_btn.setEnabled(self._state == STATE_WAITING_OPERATOR)
        self.abort_btn.setEnabled(running)

        for widget in (self.home_btn, self.goto_btn, self.goto_spin):
            widget.setEnabled(has_stage and not running)
        self.stop_btn.setEnabled(has_stage)
        for widget in (self.apply_btn, self.test_btn):
            widget.setEnabled(has_camera and not running)

        # Green and clickable only while the device is open. Hardware > Stage /
        # Camera still opens the panel when it is not, so a manual URI is
        # reachable without a connection.
        for button, ok, what in ((self.stage_btn, has_stage, "stage"),
                                 (self.camera_btn, has_camera, "camera")):
            button.setEnabled(ok)
            button.setStyleSheet(HARDWARE_OK_STYLE if ok else HARDWARE_BAD_STYLE)
            button.setToolTip(
                f"{what.capitalize()} connected - open the {what} panel" if ok
                else f"No {what} connected. Press Reconnect, or use the "
                     f"Hardware menu to open the panel anyway."
            )

        self.out_dir_prompt.setVisible(not self.out_dir.text().strip())

        if not has_stage:
            tip = "No stage connected."
        elif not has_camera:
            tip = "No camera connected."
        elif running:
            tip = "A sequence is running."
        elif getattr(self, "_blocking_errors", True):
            tip = "Fix the problems listed above the buttons."
        else:
            tip = "Run the sequence."
        self.start_btn.setToolTip(tip)

    def _start(self):
        plan = self._plan()
        errors = [p.text for p in validate(plan, self._limits) if p.level == "error"]
        if errors:
            QMessageBox.warning(self, "Cannot start", "\n".join(errors))
            return
        self.position_model.reset_status()
        self.progress.setMaximum(plan.frame_count)
        self.progress.setValue(0)
        self.log.append_line(
            f"starting: {len(plan.targets)} positions, {plan.frame_count} frames "
            f"-> {plan.out_dir}"
        )
        self.worker.post(commands.RunSequence(plan))

    def _continue(self):
        self.worker.operator_continue()

    def _abort(self):
        self.worker.request_abort()
        self.log.append_line("abort requested", logging.WARNING)

    def _choose_out_dir(self):
        start = self.out_dir.text() or self.settings.value("last_out_dir", "", type=str)
        directory = QFileDialog.getExistingDirectory(self, "Output directory", start)
        if directory:
            self.out_dir.setText(directory)
            self.settings.setValue("last_out_dir", directory)

    def _selected_rows(self):
        return sorted(i.row() for i in self.position_view.selectionModel().selectedRows())

    def _add_position(self):
        rows = self._selected_rows()
        targets = self.position_model.targets
        after = rows[-1] if rows else None
        if targets:
            value = targets[after] if after is not None else targets[-1]
        else:
            # Somewhere inside the travel, so the back-off check can pass.
            value = self._limits[0] + DEFAULT_BACKOFF if self._limits else 285000
        new_row = self.position_model.add(value, after)
        self.position_view.selectRow(new_row)
        self.position_view.edit(self.position_model.index(new_row, 1))

    def _remove_position(self):
        rows = self._selected_rows()
        if not rows:
            # Nothing selected: drop the last one, which is what "-" means to
            # someone trimming a six-position default down to two.
            if self.position_model.rowCount():
                rows = [self.position_model.rowCount() - 1]
            else:
                return
        for row in reversed(rows):
            self.position_model.remove(row)
        remaining = self.position_model.rowCount()
        if remaining:
            self.position_view.selectRow(min(rows[0], remaining - 1))

    def _clear_positions(self):
        self.position_model.set_targets([])

    def _move_position(self, delta):
        rows = self._selected_rows()
        if rows:
            self.position_view.selectRow(self.position_model.move(rows[0], delta))


    def locate_dll(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate ASICamera2.dll", "", "ASICamera2.dll;;DLL (*.dll)")
        if not path:
            return
        from fifa_gui.hardware.asi_loader import remember_path

        remember_path(path)
        self.log.append_line(f"camera DLL set to {path}")
        self._reconnect()

    def _reconnect(self):
        self.banner_label.setText("Looking for hardware ...")
        self.banner.setVisible(True)
        self.worker.post(
            commands.Connect(stage_uri=self.stage_uri.currentText().strip())
        )

    def _show_problems(self):
        if not self._problems:
            QMessageBox.information(self, "Hardware", "Both devices are connected.")
            return
        parts = []
        for device, message, remedy in self._problems:
            block = f"<b>{device.title()}</b><br>{message}"
            if remedy:
                block += "<br><br>" + remedy.replace("\n", "<br>")
            parts.append(block)
        QMessageBox.information(self, "Hardware problems", "<br><br>".join(parts))

    # ----------------------------------------------------------- plan files

    def save_plan(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save plan", "plan.json", "JSON (*.json)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self._plan().to_dict(), fh, indent=2)
        self.log.append_line(f"plan saved to {path}")

    def load_plan(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load plan", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                plan = SequencePlan.from_dict(json.load(fh))
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Cannot load plan", str(exc))
            return
        self._apply_plan(plan)
        self.log.append_line(f"plan loaded from {path}")

    def _apply_plan(self, plan):
        self.position_model.set_targets(plan.targets)
        self.out_dir.setText(plan.out_dir)
        self.overwrite_check.setChecked(plan.overwrite)
        self.gain.setValue(plan.camera.gain)
        self.offset.setValue(plan.camera.offset)
        self.exposure.setValue(int(plan.camera.exposure_us))
        self.image_type.setCurrentText(plan.camera.image_type)
        self.cooler.setChecked(plan.camera.cooler_on)
        self.target_temp.setValue(plan.camera.target_temp_c)
        self._revalidate()

    # ------------------------------------------------------------ settings

    def _restore_settings(self):
        s = self.settings
        geometry = s.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        # The output directory is deliberately NOT restored: it must be named
        # for the unit being measured, and a remembered path silently writes
        # the next sweep into the previous unit's folder.
        self.gain.setValue(int(s.value("gain", DEFAULT_GAIN)))
        self.offset.setValue(int(s.value("offset", DEFAULT_OFFSET)))
        self.exposure.setValue(int(s.value("exposure_us", DEFAULT_EXPOSURE_US)))
        # The stage URI is NOT restored. It is whatever is attached now, and
        # a remembered one only ever sends the app looking for a controller
        # that has moved or never existed. Type one to override per session.
        # The position list is deliberately NOT restored: every run starts
        # from the standard sweep. Use File > Save/Load plan to keep a
        # custom list.
        self._revalidate()

    def _save_settings(self):
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("gain", self.gain.value())
        s.setValue("offset", self.offset.value())
        s.setValue("exposure_us", self.exposure.value())

    def _about(self):
        QMessageBox.about(
            self, "About FIFA Calibrate",
            f"<b>FIFA Calibrate {__version__}</b><br><br>"
            "Drives the ZWO ASI camera and the Standa/XIMC stage through a "
            "sweep of positions, saving one full frame per position.<br><br>"
            "Frames are named after the position the stage actually reached."
        )
