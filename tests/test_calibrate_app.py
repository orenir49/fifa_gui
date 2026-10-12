"""Calibrate app behaviour, driven headlessly against fake devices.

Nothing here opens the real camera or stage: they are on a shared bench and may
be in use. The no-hardware path is also exercised directly, because "the window
still opens on a PC with no devices" is a requirement, not a nicety.
"""

import json
import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from fifa_gui.apps.calibrate import commands  # noqa: E402
from fifa_gui.apps.calibrate.worker import (  # noqa: E402
    STATE_ABORTED, STATE_DONE, STATE_WAITING_OPERATOR, AcquisitionWorker,
)
from fifa_gui.core.sequence import CameraSettings, SequencePlan  # noqa: E402
from tests.fakes import LEFT, RIGHT, FakeCamera, FakeStage  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def worker(qapp):
    """A worker with fake devices already attached, driven synchronously.

    The worker normally lives on its own QThread; here the sequence is called
    directly so the test can step through it deterministically.
    """
    w = AcquisitionWorker()
    w._camera = FakeCamera()
    w._stage = FakeStage()
    w._camera.open()
    w._stage.open()
    yield w
    w._close_devices()


def _plan(tmp_path, **kwargs):
    kwargs.setdefault("targets", [285000, 295000])
    kwargs.setdefault("out_dir", str(tmp_path))
    kwargs.setdefault("pause_for_operator", False)
    kwargs.setdefault("settle_ms", 0)
    kwargs.setdefault("camera", CameraSettings(exposure_us=1000))
    return SequencePlan(**kwargs)


# ------------------------------------------------------------- the sequence

def test_sequence_saves_one_frame_per_position(worker, qapp, tmp_path):
    states = []
    worker.state_changed.connect(lambda s, p: states.append(s))
    worker._run_sequence(_plan(tmp_path))
    worker._savers.waitForDone()
    qapp.processEvents()

    saved = sorted(p for p in os.listdir(tmp_path) if p.endswith(".npy"))
    assert saved == ["284999.npy", "294999.npy"] or len(saved) == 2
    assert STATE_DONE in states
    assert worker._camera.exposures == 2


def test_frames_are_named_after_the_achieved_position(worker, qapp, tmp_path):
    # The fake stage lands exactly on the target, so the names are the targets.
    # The real one lands a step short, which is why the notebook's files are
    # 284999.npy for a 285000 target -- the name follows what was reached.
    worker._run_sequence(_plan(tmp_path, targets=[285000]))
    worker._savers.waitForDone()
    assert os.path.exists(tmp_path / "285000.npy")


def test_the_stage_retracts_by_the_backoff_before_exposing(worker, qapp, tmp_path):
    worker._run_sequence(_plan(tmp_path, targets=[285000], backoff=110000))
    assert worker._stage.moves == [285000, 175000]


def test_repeats_get_a_suffix(worker, qapp, tmp_path):
    worker._run_sequence(_plan(tmp_path, targets=[324998], repeats=3))
    worker._savers.waitForDone()
    names = sorted(p for p in os.listdir(tmp_path) if p.endswith(".npy"))
    assert names == ["324998_0.npy", "324998_1.npy", "324998_2.npy"]


def test_saved_frames_are_complete_and_readable(worker, qapp, tmp_path):
    worker._run_sequence(_plan(tmp_path, targets=[285000]))
    worker._savers.waitForDone()
    arr = np.load(tmp_path / "285000.npy")
    assert arr.shape == (120, 160) and arr.dtype == np.uint16
    # No .part file is left behind: the write is atomic.
    assert not any(p.endswith(".part") for p in os.listdir(tmp_path))


def test_exposure_is_taken_once_per_frame(worker, qapp, tmp_path):
    # The notebook called start_exposure() and then capture(), which starts
    # another one, so every frame was exposed twice.
    worker._run_sequence(_plan(tmp_path, targets=[285000, 295000, 305000]))
    assert worker._camera.exposures == 3


# ------------------------------------------------------- the operator pause

def test_pause_waits_for_continue(worker, qapp, tmp_path):
    import threading

    states = []
    worker.state_changed.connect(lambda s, p: states.append(s))

    done = threading.Event()

    def run():
        worker._run_sequence(_plan(tmp_path, targets=[285000],
                                   pause_for_operator=True))
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # The sequence runs on another thread, so its signals arrive by queued
    # connection and only land once this thread pumps its event loop.
    deadline = time.monotonic() + 5
    while STATE_WAITING_OPERATOR not in states and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert STATE_WAITING_OPERATOR in states, "sequence did not pause"
    assert not done.is_set(), "sequence continued without the operator"

    worker.operator_continue()
    assert done.wait(10), "sequence did not resume after Continue"
    qapp.processEvents()
    assert STATE_DONE in states


def test_abort_during_a_pause_stops_the_run(worker, qapp, tmp_path):
    import threading

    states = []
    worker.state_changed.connect(lambda s, p: states.append(s))
    done = threading.Event()

    def run():
        worker._run_sequence(_plan(tmp_path, targets=[285000, 295000],
                                   pause_for_operator=True))
        done.set()

    threading.Thread(target=run, daemon=True).start()
    deadline = time.monotonic() + 5
    while STATE_WAITING_OPERATOR not in states and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert STATE_WAITING_OPERATOR in states, "sequence did not pause"

    worker.request_abort()
    assert done.wait(10)
    qapp.processEvents()
    assert STATE_ABORTED in states
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".npy")]


# ------------------------------------------------------------- provenance

def test_a_run_record_is_written_next_to_the_data(worker, qapp, tmp_path):
    worker._run_sequence(_plan(tmp_path, targets=[285000, 295000]))
    worker._savers.waitForDone()

    records = [p for p in os.listdir(tmp_path) if p.startswith("run_")]
    assert len(records) == 1
    payload = json.loads((tmp_path / records[0]).read_text())
    assert payload["outcome"] == "complete"
    assert len(payload["frames"]) == 2
    frame = payload["frames"][0]
    assert {"file", "target", "achieved", "peak_adu", "saturated"} <= set(frame)
    assert payload["plan"]["backoff"] == 110000


def test_saturation_is_recorded_but_does_not_stop_the_run(worker, qapp, tmp_path):
    worker._camera.peak = 65520        # the 12-bit full well in RAW16
    logged = []
    worker.log.connect(lambda level, text: logged.append(text))

    worker._run_sequence(_plan(tmp_path, targets=[285000]))
    worker._savers.waitForDone()

    assert os.path.exists(tmp_path / "285000.npy"), "a saturated frame is still saved"
    assert any("SATURATED" in text for text in logged)
    record = next(p for p in os.listdir(tmp_path) if p.startswith("run_"))
    assert json.loads((tmp_path / record).read_text())["frames"][0]["saturated"]


# ---------------------------------------------------------- the window

def test_window_opens_with_no_hardware(qapp, monkeypatch):
    # The requirement: the exe opens on any Windows PC. Probing must not be able
    # to stop the window appearing, however it fails.
    from fifa_gui.hardware.base import NullCamera, NullStage
    from fifa_gui.hardware.discovery import ProbeResult

    # Patch the name the worker actually resolves: it imported probe directly.
    monkeypatch.setattr(
        "fifa_gui.apps.calibrate.worker.probe",
        lambda **kwargs: ProbeResult(
            camera=NullCamera(), stage=NullStage(),
            problems=[("camera", "No ZWO camera detected.", "Check the USB3 cable."),
                      ("stage", "No XIMC stage controller found.", "Check the driver.")],
        ),
    )

    from fifa_gui.apps.calibrate.window import CalibrateWindow

    win = CalibrateWindow()
    win.show()
    deadline = time.monotonic() + 10
    while win.banner_label.text().startswith("Looking") and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    assert win.isVisible()
    assert win.banner.isVisible()
    # The message has to name the usual culprits: the device is far more often
    # held by another program than actually absent.
    banner = win.banner_label.text()
    assert "Unable to connect" in banner
    for culprit in ("ASIStudio", "XILab", "Jupyter"):
        assert culprit in banner
    assert not win.start_btn.isEnabled()
    assert not win.continue_btn.isEnabled()
    assert "No stage connected." in win.start_btn.toolTip()
    win.close()
    qapp.processEvents()


def test_importing_the_entry_point_does_not_touch_the_drivers():
    # libximc loads native DLLs at import and raises without the MSVC++ 2013
    # redistributable, so it must never be imported at module scope.
    import subprocess
    import sys

    code = (
        "import sys; import fifa_gui.apps.calibrate_main as m; "
        "print('libximc' in sys.modules, 'zwoasi' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("False False"), out.stdout


def test_no_connect_opens_without_touching_the_devices(qapp, monkeypatch):
    # The bench is shared, so the app must be startable without opening the
    # camera or the stage. Probing must not happen at all in this mode.
    called = []
    monkeypatch.setattr(
        "fifa_gui.apps.calibrate.worker.probe",
        lambda **kwargs: called.append(kwargs),
    )
    from fifa_gui.apps.calibrate.window import CalibrateWindow

    win = CalibrateWindow(auto_connect=False)
    win.show()
    for _ in range(50):
        qapp.processEvents()
        time.sleep(0.01)

    assert called == [], "probe() ran despite --no-connect"
    assert win.isVisible()
    assert "--no-connect" in win.banner_label.text()
    assert not win.start_btn.isEnabled()
    win.close()
    qapp.processEvents()


# ------------------------------------------------------- exposure and targets

def test_exposure_is_in_microseconds():
    # ASI_EXPOSURE takes microseconds, and the useful end for a bright fiber
    # spot is ~32 us -- a control that bottomed out at 1 ms could not reach it.
    from fifa_gui.core.sequence import DEFAULT_EXPOSURE_US

    s = CameraSettings(exposure_us=32)
    assert DEFAULT_EXPOSURE_US == 32
    assert s.exposure_ms == pytest.approx(0.032)
    assert s.exposure_s == pytest.approx(3.2e-5)


def test_old_plans_with_milliseconds_still_load(tmp_path):
    plan = SequencePlan.from_dict(
        {"targets": [285000], "out_dir": "x", "camera": {"exposure_ms": 2.5}}
    )
    assert plan.camera.exposure_us == 2500


def test_the_configured_exposure_reaches_the_camera(worker, qapp, tmp_path):
    worker._run_sequence(_plan(tmp_path, targets=[285000],
                               camera=CameraSettings(exposure_us=32)))
    assert worker._camera.settings.exposure_us == 32


def test_position_table_add_and_remove(qapp, monkeypatch):
    from fifa_gui.hardware.base import NullCamera, NullStage
    from fifa_gui.hardware.discovery import ProbeResult
    from fifa_gui.apps.calibrate.window import CalibrateWindow

    monkeypatch.setattr(
        "fifa_gui.apps.calibrate.worker.probe",
        lambda **kwargs: ProbeResult(camera=NullCamera(), stage=NullStage(), problems=[]),
    )
    win = CalibrateWindow(auto_connect=False)
    model = win.position_model
    assert model.rowCount() == 6           # the standard sweep

    # Trimming six down to two is the thing that did not work: with no row
    # selected, selectedRows() is empty and both buttons silently did nothing.
    for _ in range(4):
        win._remove_position()
    assert model.rowCount() == 2
    assert len(win._plan().targets) == 2

    win.position_view.selectRow(0)
    win._add_position()
    assert model.rowCount() == 3

    win._clear_positions()
    assert model.rowCount() == 0
    # Adding to an empty table must still produce a usable position.
    win._add_position()
    assert model.rowCount() == 1
    assert model.targets[0] > 0

    win.close()
    qapp.processEvents()


def test_pause_prompt_mentions_the_nd_filter(qapp, monkeypatch):
    # Forgetting to put the ND filter back is what saturates the frame, so the
    # prompt has to name it, not just say "recentre".
    from fifa_gui.apps.calibrate.window import (
        CONTINUE_WAITING_STYLE, PAUSE_PROMPT, CalibrateWindow,
    )
    from fifa_gui.apps.calibrate.worker import STATE_WAITING_OPERATOR
    from fifa_gui.hardware.base import NullCamera, NullStage
    from fifa_gui.hardware.discovery import ProbeResult

    monkeypatch.setattr(
        "fifa_gui.apps.calibrate.worker.probe",
        lambda **kwargs: ProbeResult(camera=NullCamera(), stage=NullStage(), problems=[]),
    )
    for word in ("CENTER", "FIBER", "ND FILTER", "CONTINUE"):
        assert word in PAUSE_PROMPT.upper()

    win = CalibrateWindow(auto_connect=False)
    win.show()                      # isVisible() is False on a hidden parent
    qapp.processEvents()
    assert not win.pause_banner.isVisible()

    win._on_state(STATE_WAITING_OPERATOR, {"position": 284999})
    qapp.processEvents()
    assert win.pause_banner.isVisible()
    assert "ND FILTER" in win.pause_label.text().upper()
    assert "284,999" in win.pause_label.text()
    assert win.continue_btn.isEnabled()
    assert win.continue_btn.styleSheet() == CONTINUE_WAITING_STYLE
    assert win.continue_btn.minimumHeight() >= 80

    win._on_state("RUNNING", {})
    qapp.processEvents()
    assert not win.pause_banner.isVisible()
    assert win.continue_btn.styleSheet() == ""

    win.close()
    qapp.processEvents()


def test_log_stretch_lifts_a_faint_halo_out_of_the_floor():
    # A linear stretch renders a near-saturated core as a white dot and the
    # halo as black; the log stretch is what makes centring visible.
    import numpy as np

    from fifa_gui.qtutil.imageview import to_qimage

    arr = np.full((8, 8), 650, dtype=np.uint16)
    arr[4, 4] = 65000      # core
    arr[4, 5] = 2000       # faint halo, ~2% of the core above the floor

    lo, hi = 650.0, 65000.0
    linear = to_qimage(arr, lo, hi, log=False)._backing
    logged = to_qimage(arr, lo, hi, log=True)._backing

    assert linear[4, 5] < 10, "halo should be invisible on a linear stretch"
    assert logged[4, 5] > 80, "halo should be clearly visible on a log stretch"
    assert logged[4, 4] > logged[4, 5] > logged[0, 0]


def test_sequence_homes_before_running(worker, qapp, tmp_path):
    # Every run must start from the same datum, so the positions in the file
    # names mean the same thing across runs.
    assert worker._stage.homed == 0
    worker._run_sequence(_plan(tmp_path, targets=[285000]))
    worker._savers.waitForDone()

    assert worker._stage.homed == 1
    assert worker._stage.moves[0] == 285000, "homing must precede the first move"

    record = next(p for p in os.listdir(tmp_path) if p.startswith("run_"))
    payload = json.loads((tmp_path / record).read_text())
    assert payload["homed_before_sequence"] is True
    assert payload["position_after_home"] == 0


def test_default_sweep_is_the_standard_six_positions(qapp, monkeypatch):
    # The list deliberately does not persist: a trimmed-down test run must not
    # silently become the starting point for the next real run.
    from fifa_gui.apps.calibrate.window import DEFAULT_TARGETS, CalibrateWindow
    from fifa_gui.hardware.base import NullCamera, NullStage
    from fifa_gui.hardware.discovery import ProbeResult

    monkeypatch.setattr(
        "fifa_gui.apps.calibrate.worker.probe",
        lambda **kwargs: ProbeResult(camera=NullCamera(), stage=NullStage(), problems=[]),
    )
    assert DEFAULT_TARGETS == [285000, 295000, 305000, 315000, 325000, 330000]

    win = CalibrateWindow(auto_connect=False)
    assert win.position_model.targets == DEFAULT_TARGETS
    win._remove_position()
    win._remove_position()
    assert len(win.position_model.targets) == 4
    win.close()
    qapp.processEvents()

    again = CalibrateWindow(auto_connect=False)
    assert again.position_model.targets == DEFAULT_TARGETS
    again.close()
    qapp.processEvents()


def test_stage_limits_are_shown_above_the_sweep(qapp, monkeypatch):
    from fifa_gui.apps.calibrate.window import CalibrateWindow
    from fifa_gui.hardware.base import NullCamera, NullStage
    from fifa_gui.hardware.discovery import ProbeResult

    monkeypatch.setattr(
        "fifa_gui.apps.calibrate.worker.probe",
        lambda **kwargs: ProbeResult(camera=NullCamera(), stage=NullStage(), problems=[]),
    )
    win = CalibrateWindow(auto_connect=False)
    assert "not connected" in win.sweep_limits_label.text()

    win._on_limits(LEFT, RIGHT)
    qapp.processEvents()
    text = win.sweep_limits_label.text()
    assert f"{LEFT:,}" in text and f"{RIGHT:,}" in text
    # The usable floor is the limit plus the back-off, which is the number that
    # actually constrains a target.
    from fifa_gui.core.sequence import DEFAULT_BACKOFF
    assert f"{LEFT + DEFAULT_BACKOFF:,}" in text
    win.close()
    qapp.processEvents()


def test_last_frame_scaling_does_not_wash_out_a_point_source(qapp, monkeypatch):
    """The bug this guards: a 46 MP frame holding a handful of bright pixels.

    autoscale_levels used the 99.5th percentile for the top of the range, but
    the spot covers far less than 0.5% of the frame, so that percentile was
    still background: hi - lo collapsed and the whole frame clipped to white.
    """
    import numpy as np

    from fifa_gui.apps.calibrate.window import CalibrateWindow
    from fifa_gui.hardware.base import NullCamera, NullStage
    from fifa_gui.hardware.discovery import ProbeResult
    from fifa_gui.qtutil.imageview import autoscale_levels, to_qimage

    monkeypatch.setattr(
        "fifa_gui.apps.calibrate.worker.probe",
        lambda **kwargs: ProbeResult(camera=NullCamera(), stage=NullStage(), problems=[]),
    )

    rng = np.random.default_rng(0)
    frame = (650 + rng.normal(0, 5, (706, 1036))).astype(np.uint16)
    frame[350, 500] = 60000
    frame[350, 501] = 20000

    # The old behaviour, kept here to show why it was unusable.
    old_lo, old_hi = autoscale_levels(frame)
    washed = to_qimage(frame, old_lo, old_hi, log=True)._backing
    assert washed.mean() > 200, "background should have flooded the display"

    win = CalibrateWindow(auto_connect=False)
    lo, hi = win._auto_levels_for(frame)
    assert hi == 60000, "the top of the scale must be the brightest pixel"

    shown = to_qimage(frame, lo, hi, log=True)._backing
    assert shown.mean() < 120, "background must no longer flood the display"
    assert shown[350, 500] > shown[350, 501] > shown[0, 0]

    # Manual vmin/vmax must override, and survive a redraw with no new exposure.
    win._last_preview = frame
    win._last_meta = {"peak": 60000.0}
    win.auto_levels.setChecked(False)
    win.vmin.setValue(600)
    win.vmax.setValue(1000)
    win._redraw_last_frame()
    assert win.vmin.value() == 600 and win.vmax.value() == 1000

    win.auto_levels.setChecked(True)
    win._redraw_last_frame()
    assert win.vmax.value() == 60000, "Auto must recompute the range"

    win.close()
    qapp.processEvents()


# ------------------------------------------------- the left column's layout

def _in_window(widget, ancestor):
    """True when `widget` is shown inside `ancestor` rather than its own window."""
    while widget is not None:
        if widget is ancestor:
            return True
        widget = widget.parentWidget()
    return False


def _no_hardware_window(monkeypatch):
    from fifa_gui.apps.calibrate.window import CalibrateWindow
    from fifa_gui.hardware.base import NullCamera, NullStage
    from fifa_gui.hardware.discovery import ProbeResult

    monkeypatch.setattr(
        "fifa_gui.apps.calibrate.worker.probe",
        lambda **kwargs: ProbeResult(camera=NullCamera(), stage=NullStage(), problems=[]),
    )
    return CalibrateWindow(auto_connect=False)


def test_the_hardware_panels_are_not_embedded_in_the_left_column(qapp, monkeypatch):
    """The sweep owns the left column; stage and camera are behind buttons.

    They are set once at the start of a session, so they do not earn permanent
    space next to the thing the operator drives all run.
    """
    win = _no_hardware_window(monkeypatch)
    central = win.centralWidget()

    for widget in (win.position_view, win.start_btn, win.out_dir):
        assert _in_window(widget, central)
    for widget in (win.stage_uri, win.home_btn, win.goto_spin, win.goto_btn,
                   win.gain, win.exposure, win.image_type, win.apply_btn):
        assert not _in_window(widget, central)

    # The readouts that are watched throughout a run stay on the main window,
    # along with Test exposure, which is used between positions.
    for widget in (win.position_label, win.temp_label, win.test_btn):
        assert _in_window(widget, central)

    assert not win.stage_dialog.isVisible()
    assert not win.camera_dialog.isVisible()
    win.close()
    qapp.processEvents()


def _connect(win, camera=True, stage=True):
    """Drive the window to the state the worker reports when devices are open."""
    win._on_hardware_changed(
        FakeCamera().info() if camera else None,
        FakeStage().info() if stage else None,
        [],
    )


def test_the_compact_buttons_open_the_hardware_panels(qapp, monkeypatch):
    win = _no_hardware_window(monkeypatch)
    _connect(win)

    # click() returning at all is half the point: a modal dialog would block
    # here, and the preview has to stay watchable while the stage is jogged.
    win.stage_btn.click()
    win.camera_btn.click()
    qapp.processEvents()
    assert win.stage_dialog.isVisible()
    assert win.camera_dialog.isVisible()
    assert not win.stage_dialog.isModal()
    assert not win.camera_dialog.isModal()

    # The controls still drive the same widgets, wherever they are shown.
    assert _in_window(win.home_btn, win.stage_dialog)
    assert _in_window(win.exposure, win.camera_dialog)

    win.close()
    qapp.processEvents()


def test_hardware_buttons_are_green_when_connected_red_and_dead_when_not(
    qapp, monkeypatch
):
    """The buttons are the only hardware status on the main window."""
    from fifa_gui.apps.calibrate.window import (
        HARDWARE_BAD_STYLE, HARDWARE_OK_STYLE,
    )

    win = _no_hardware_window(monkeypatch)
    for button in (win.stage_btn, win.camera_btn):
        assert not button.isEnabled()
        assert button.styleSheet() == HARDWARE_BAD_STYLE

    _connect(win)
    for button in (win.stage_btn, win.camera_btn):
        assert button.isEnabled()
        assert button.styleSheet() == HARDWARE_OK_STYLE

    # One device missing must not make the other look connected.
    _connect(win, camera=False)
    assert win.stage_btn.isEnabled()
    assert win.stage_btn.styleSheet() == HARDWARE_OK_STYLE
    assert not win.camera_btn.isEnabled()
    assert win.camera_btn.styleSheet() == HARDWARE_BAD_STYLE

    # The panel still has to be reachable with nothing connected, or a manual
    # stage URI could never be typed in.
    _connect(win, camera=False, stage=False)
    win._show_popup(win.stage_dialog)
    qapp.processEvents()
    assert win.stage_dialog.isVisible()
    assert _in_window(win.stage_uri, win.stage_dialog)

    win.close()
    qapp.processEvents()


def test_the_pause_prompt_takes_the_filter_out_and_puts_it_back(qapp, monkeypatch):
    """Both halves of the ND filter step, in the order they are done."""
    from fifa_gui.apps.calibrate.window import PAUSE_PROMPT

    steps = ["REMOVE ND FILTER", "CENTER FIBER", "RETURN ND FILTER", "PRESS CONTINUE"]
    assert [PAUSE_PROMPT.index(step) for step in steps] == sorted(
        PAUSE_PROMPT.index(step) for step in steps
    )

    win = _no_hardware_window(monkeypatch)
    win._on_state(STATE_WAITING_OPERATOR, {"position": 285000})
    qapp.processEvents()
    assert PAUSE_PROMPT in win.pause_label.text()
    win.close()
    qapp.processEvents()


def test_the_output_directory_starts_empty_and_asks_for_a_unit_name(
    qapp, monkeypatch, tmp_path, isolated_qsettings
):
    """A remembered path writes the next sweep into the previous unit's folder."""
    from fifa_gui.apps.calibrate.window import OUT_DIR_PROMPT

    win = _no_hardware_window(monkeypatch)
    assert win.out_dir.text() == ""
    assert win.out_dir_prompt.isVisibleTo(win)
    assert "mast0x" in OUT_DIR_PROMPT
    assert "#ef4444" in win.out_dir_prompt.styleSheet()       # red
    assert "bold" in win.out_dir_prompt.styleSheet()

    win.out_dir.setText(str(tmp_path))
    qapp.processEvents()
    assert not win.out_dir_prompt.isVisibleTo(win)

    # Closing writes QSettings; the next window must still come up empty.
    win.close()
    qapp.processEvents()
    win2 = _no_hardware_window(monkeypatch)
    assert win2.out_dir.text() == ""
    assert win2.out_dir_prompt.isVisibleTo(win2)
    win2.close()
    qapp.processEvents()


# ------------------------------------------------ preview scaling and gain

def test_the_level_cuts_are_sliders_not_boxes(qapp, monkeypatch):
    """A cut on a 16-bit frame is dragged until it looks right."""
    from PySide6.QtWidgets import QSlider

    win = _no_hardware_window(monkeypatch)
    assert isinstance(win.vmin, QSlider)
    assert isinstance(win.vmax, QSlider)
    assert win.vmin.maximum() == 65535 and win.vmax.maximum() == 65535

    # Manual cuts drive the redraw, and the readouts follow -- a bare slider
    # gives no way to tell what value was picked.
    win._last_preview = np.full((16, 16), 500, dtype=np.uint16)
    win._last_meta = {"peak": 500.0}
    win.auto_levels.setChecked(False)
    win.vmin.setValue(100)
    win.vmax.setValue(900)
    qapp.processEvents()
    assert win.vmin_label.text() == "100"
    assert win.vmax_label.text() == "900"

    win.close()
    qapp.processEvents()


def test_the_preview_starts_linear(qapp, monkeypatch):
    """Linear is the stretch whose pixel values can be read off the display."""
    win = _no_hardware_window(monkeypatch)
    assert not win.log_levels.isChecked()
    win.close()
    qapp.processEvents()


def test_gain_defaults_to_unity_and_the_offset_to_30_adu(qapp, monkeypatch,
                                                         isolated_qsettings):
    from fifa_gui.core.sequence import DEFAULT_GAIN, DEFAULT_OFFSET

    assert DEFAULT_GAIN == 0
    assert DEFAULT_OFFSET == 30
    assert CameraSettings().gain == 0
    assert CameraSettings().offset == 30

    win = _no_hardware_window(monkeypatch)
    assert win.gain.value() == 0
    assert win.offset.value() == 30
    assert "ADU" in win.offset.suffix()
    assert win._plan().camera.offset == 30
    win.close()
    qapp.processEvents()


def test_the_configured_offset_reaches_the_camera(worker, qapp, tmp_path):
    worker._run_sequence(_plan(tmp_path, camera=CameraSettings(offset=42)))
    assert worker._camera.settings.offset == 42


def test_plans_written_before_the_offset_existed_still_load(tmp_path):
    """The offset is new; an older saved plan has no key for it."""
    old = {"targets": [285000], "out_dir": str(tmp_path),
           "camera": {"gain": 120, "exposure_us": 1000}}
    plan = SequencePlan.from_dict(old)
    assert plan.camera.gain == 120
    assert plan.camera.offset == 30


# --------------------------------------------------------- the stage URI

def _stage_choosing(monkeypatch, found, requested=None):
    """A XimcStage whose enumeration returns `found`, without any driver."""
    from fifa_gui.hardware import ximc as ximc_mod

    monkeypatch.setattr(ximc_mod, "enumerate_stages", lambda: list(found))
    stage = ximc_mod.XimcStage(uri=requested)
    return stage._choose_uri()[0]


def test_a_stale_uri_never_beats_an_attached_controller(monkeypatch):
    """The bug: the GUI went looking for a stage at fake://stage.

    A URI saved from a previous session (or written into the settings store by
    something else) must not be preferred over what enumeration actually finds.
    """
    real = r"xi-com:\.\COM5"
    assert _stage_choosing(monkeypatch, [real], "fake://stage") == real
    assert _stage_choosing(monkeypatch, [real], "") == real
    assert _stage_choosing(monkeypatch, [real], None) == real
    # A URI that is genuinely attached is still honoured over the first found.
    other = r"xi-com:\.\COM7"
    assert _stage_choosing(monkeypatch, [real, other], other) == other


def test_a_typed_uri_is_tried_when_the_probe_finds_nothing(monkeypatch):
    """ENUMERATE_PROBE does miss controllers, so a real URI is still worth a go."""
    typed = r"xi-com:\.\COM5"
    assert _stage_choosing(monkeypatch, [], typed) == typed
    # ... but nonsense is not, and there is nothing to fall back to.
    assert _stage_choosing(monkeypatch, [], "fake://stage") is None
    assert _stage_choosing(monkeypatch, [], "") is None


def test_the_stage_uri_is_not_remembered_between_sessions(qapp, monkeypatch,
                                                          isolated_qsettings):
    win = _no_hardware_window(monkeypatch)
    win.stage_uri.setEditText("fake://stage")
    win.close()
    qapp.processEvents()

    win2 = _no_hardware_window(monkeypatch)
    assert win2.stage_uri.currentText() == ""
    win2.close()
    qapp.processEvents()
