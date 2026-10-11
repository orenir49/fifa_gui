"""The Calibrate window: drive the stage and camera through a position sweep."""

import json
import logging
import os

import numpy as np
from PySide6.QtCore import QSettings, Qt, QThread, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTableView, QVBoxLayout, QWidget,
)

from fifa_gui import __version__
from fifa_gui.apps.calibrate import commands
from fifa_gui.apps.calibrate.positiontable import GenerateDialog, PositionTableModel
from fifa_gui.apps.calibrate.worker import (
    STATE_ABORTED, STATE_DONE, STATE_FAILED, STATE_IDLE, STATE_RUNNING,
    STATE_WAITING_OPERATOR, AcquisitionWorker,
)
from fifa_gui.core.sequence import (
    DEFAULT_BACKOFF, DEFAULT_SETTLE_MS, CameraSettings, SequencePlan,
    generate_targets, validate,
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
    "QFrame { background: #78350f; border: 1px solid #f59e0b; border-radius: 4px; }"
    "QLabel { color: #fef3c7; font-weight: bold; font-size: 11pt; }"
)


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

        left = QScrollArea()
        left.setWidgetResizable(True)
        left.setFixedWidth(430)
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.addWidget(self._build_stage_box())
        panel_layout.addWidget(self._build_camera_box())
        panel_layout.addWidget(self._build_sequence_box())
        panel_layout.addStretch(1)
        left.setWidget(panel)

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

    def _build_stage_box(self):
        box = QGroupBox("Stage")
        layout = QVBoxLayout(box)

        self.stage_uri = QComboBox()
        self.stage_uri.setEditable(True)
        self.stage_uri.lineEdit().setPlaceholderText(r"auto-detect, or xi-com:\\.\COM5")

        self.position_label = QLabel("position: -")
        self.position_label.setStyleSheet("font-family: Consolas, monospace; font-size: 13pt;")
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
        layout.addWidget(self.position_label)
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

        self.gain = QSpinBox(); self.gain.setRange(0, 600); self.gain.setValue(120)
        self.exposure = QSpinBox()
        self.exposure.setRange(1, 600_000)
        self.exposure.setValue(100)
        self.exposure.setSuffix(" ms")
        self.exposure.setToolTip(
            "The notebook never set this, so frames ran at whatever the SDK\n"
            "default was -- which is how the archived datasets came out saturated."
        )
        self.image_type = QComboBox()
        self.image_type.addItems(["RAW16", "RAW8"])

        self.cooler = QCheckBox("Cooler on")
        self.target_temp = QSpinBox()
        self.target_temp.setRange(-40, 30)
        self.target_temp.setValue(-10)
        self.target_temp.setSuffix(" C")
        self.temp_label = QLabel("sensor: -")

        form = QFormLayout()
        form.addRow("Gain", self.gain)
        form.addRow("Exposure", self.exposure)
        form.addRow("Image type", self.image_type)
        form.addRow("", self.cooler)
        form.addRow("Target temp", self.target_temp)

        self.apply_btn = QPushButton("Apply")
        self.apply_btn.clicked.connect(
            lambda: self.worker.post(commands.ApplyCamera(self._camera_settings()))
        )
        self.test_btn = QPushButton("Test exposure")
        self.test_btn.setToolTip("Take one frame and show it, without saving")
        self.test_btn.clicked.connect(
            lambda: self.worker.post(commands.TestExposure(self._camera_settings()))
        )
        buttons = QHBoxLayout()
        buttons.addWidget(self.apply_btn)
        buttons.addWidget(self.test_btn)

        layout.addWidget(self.camera_label)
        layout.addLayout(form)
        layout.addWidget(self.temp_label)
        layout.addLayout(buttons)
        return box

    def _build_sequence_box(self):
        box = QGroupBox("Sequence")
        layout = QVBoxLayout(box)

        self.out_dir = QLineEdit()
        self.out_dir.setPlaceholderText("where the .npy frames are written")
        self.out_dir.textChanged.connect(lambda _: self._revalidate())
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._choose_out_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.out_dir, 1)
        dir_row.addWidget(browse)

        self.backoff = QSpinBox()
        self.backoff.setRange(0, 10_000_000)
        self.backoff.setValue(DEFAULT_BACKOFF)
        self.backoff.setGroupSeparatorShown(True)
        self.backoff.setToolTip("How far to retract so the obscuration clears the beam")
        self.backoff.valueChanged.connect(lambda _: self._revalidate())

        self.settle = QSpinBox()
        self.settle.setRange(0, 60_000)
        self.settle.setValue(DEFAULT_SETTLE_MS)
        self.settle.setSuffix(" ms")

        self.repeats = QSpinBox()
        self.repeats.setRange(1, 100)
        self.repeats.setValue(1)
        self.repeats.setToolTip("Frames per position; >1 names them <position>_<k>.npy")
        self.repeats.valueChanged.connect(lambda _: self._revalidate())

        self.pause_check = QCheckBox("Pause at each position to recentre the spot")
        self.pause_check.setChecked(True)
        self.overwrite_check = QCheckBox("Allow overwriting existing frames")
        self.overwrite_check.toggled.connect(lambda _: self._revalidate())

        form = QFormLayout()
        form.addRow("Back-off", self.backoff)
        form.addRow("Settle", self.settle)
        form.addRow("Repeats", self.repeats)

        self.position_model = PositionTableModel(DEFAULT_TARGETS, self)
        self.position_model.dataChanged.connect(lambda *_: self._revalidate())
        self.position_model.rowsInserted.connect(lambda *_: self._revalidate())
        self.position_model.rowsRemoved.connect(lambda *_: self._revalidate())
        self.position_view = QTableView()
        self.position_view.setModel(self.position_model)
        self.position_view.verticalHeader().setVisible(False)
        self.position_view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.position_view.setMaximumHeight(200)

        add = QPushButton("+"); add.setFixedWidth(30); add.clicked.connect(self._add_position)
        remove = QPushButton("-"); remove.setFixedWidth(30); remove.clicked.connect(self._remove_position)
        up = QPushButton("^"); up.setFixedWidth(30); up.clicked.connect(lambda: self._move_position(-1))
        down = QPushButton("v"); down.setFixedWidth(30); down.clicked.connect(lambda: self._move_position(1))
        gen = QPushButton("Generate..."); gen.clicked.connect(self._generate_positions)
        table_buttons = QHBoxLayout()
        for widget in (add, remove, up, down, gen):
            table_buttons.addWidget(widget)
        table_buttons.addStretch(1)

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
        layout.addLayout(form)
        layout.addWidget(self.pause_check)
        layout.addWidget(self.overwrite_check)
        layout.addWidget(QLabel("Positions"))
        layout.addWidget(self.position_view)
        layout.addLayout(table_buttons)
        layout.addWidget(self.validation_label)
        layout.addLayout(run_buttons)
        return box

    def _build_preview_box(self):
        box = QGroupBox("Last frame")
        layout = QVBoxLayout(box)
        self.view = ImageView()
        self.frame_label = QLabel("No frame yet. Use 'Test exposure' or start a sequence.")
        self.frame_label.setWordWrap(True)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.frame_label)
        return box

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
            self.banner_label.setText(
                "No " + " and no ".join(missing)
                + " detected - acquisition is disabled."
            )
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)
        self._update_enabled()
        self._revalidate()

    @Slot(int)
    def _on_position(self, position):
        self.position_label.setText(f"position: {position:,}")

    @Slot(int, int)
    def _on_limits(self, left, right):
        self._limits = (left, right)
        self.limits_label.setText(f"limits: {left:,} .. {right:,}")
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
            self.pause_label.setText(
                f"Recentre the spot on the fiber, then press CONTINUE.   "
                f"(stage at {position:,})"
            )
            self.pause_banner.setVisible(True)
            # The operator is looking at the bench, not the screen.
            QApplication.beep()
            QApplication.alert(self)
        else:
            self.pause_banner.setVisible(False)

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
        self.view.set_frame(preview, factor=8)
        try:
            row, col = np.unravel_index(int(np.argmax(preview)), preview.shape)
            self.view.add_marker(row * 8, col * 8, COLOR_CENTROID)
        except Exception:
            pass
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
            exposure_ms=self.exposure.value(),
            image_type=self.image_type.currentText(),
            cooler_on=self.cooler.isChecked(),
            target_temp_c=self.target_temp.value(),
        )

    def _plan(self):
        return SequencePlan(
            targets=self.position_model.targets,
            out_dir=self.out_dir.text().strip(),
            backoff=self.backoff.value(),
            settle_ms=self.settle.value(),
            repeats=self.repeats.value(),
            overwrite=self.overwrite_check.isChecked(),
            pause_for_operator=self.pause_check.isChecked(),
            camera=self._camera_settings(),
        )

    def _revalidate(self):
        problems = validate(self._plan(), self._limits)
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

    def _add_position(self):
        rows = self.position_view.selectionModel().selectedRows()
        after = rows[0].row() if rows else None
        targets = self.position_model.targets
        value = targets[after] if after is not None and targets else (
            targets[-1] if targets else 285000
        )
        new_row = self.position_model.add(value, after)
        self.position_view.selectRow(new_row)

    def _remove_position(self):
        rows = self.position_view.selectionModel().selectedRows()
        if rows:
            self.position_model.remove(rows[0].row())

    def _move_position(self, delta):
        rows = self.position_view.selectionModel().selectedRows()
        if rows:
            self.position_view.selectRow(self.position_model.move(rows[0].row(), delta))

    def _generate_positions(self):
        targets = self.position_model.targets
        dialog = GenerateDialog(
            self,
            start=targets[0] if targets else 285000,
            stop=targets[-1] if targets else 330000,
            step=10000,
            limits=self._limits,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        start, stop, step = dialog.values()
        try:
            self.position_model.set_targets(generate_targets(start, stop, step))
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot generate positions", str(exc))
            return
        self._revalidate()

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
        self.backoff.setValue(plan.backoff)
        self.settle.setValue(plan.settle_ms)
        self.repeats.setValue(plan.repeats)
        self.overwrite_check.setChecked(plan.overwrite)
        self.pause_check.setChecked(plan.pause_for_operator)
        self.gain.setValue(plan.camera.gain)
        self.exposure.setValue(int(plan.camera.exposure_ms))
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
        self.out_dir.setText(s.value("out_dir", "", type=str))
        self.backoff.setValue(int(s.value("backoff", DEFAULT_BACKOFF)))
        self.settle.setValue(int(s.value("settle_ms", DEFAULT_SETTLE_MS)))
        self.repeats.setValue(int(s.value("repeats", 1)))
        self.gain.setValue(int(s.value("gain", 120)))
        self.exposure.setValue(int(s.value("exposure_ms", 100)))
        self.pause_check.setChecked(s.value("pause_for_operator", True, type=bool))
        self.stage_uri.setEditText(s.value("stage_uri", "", type=str))
        targets = s.value("targets", "", type=str)
        if targets:
            try:
                self.position_model.set_targets(json.loads(targets))
            except ValueError:
                pass
        self._revalidate()

    def _save_settings(self):
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("out_dir", self.out_dir.text())
        s.setValue("backoff", self.backoff.value())
        s.setValue("settle_ms", self.settle.value())
        s.setValue("repeats", self.repeats.value())
        s.setValue("gain", self.gain.value())
        s.setValue("exposure_ms", self.exposure.value())
        s.setValue("pause_for_operator", self.pause_check.isChecked())
        s.setValue("stage_uri", self.stage_uri.currentText())
        s.setValue("targets", json.dumps(self.position_model.targets))

    def _about(self):
        QMessageBox.about(
            self, "About FIFA Calibrate",
            f"<b>FIFA Calibrate {__version__}</b><br><br>"
            "Drives the ZWO ASI camera and the Standa/XIMC stage through a "
            "sweep of positions, saving one full frame per position.<br><br>"
            "Frames are named after the position the stage actually reached."
        )
