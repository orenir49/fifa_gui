"""The acquisition worker: owns both devices, runs the sequence.

Lives on its own QThread and consumes a queue.Queue rather than relying on
queued slot invocations. That matters: a queued slot is only delivered when the
worker's event loop is idle, so an Abort posted during a 30 s exposure would sit
unprocessed until the exposure finished -- exactly the freeze being avoided.
Abort and Continue are threading.Events, which *are* visible mid-operation.
"""

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime

import numpy as np
from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal, Slot

from fifa_gui import __version__
from fifa_gui.apps.calibrate.commands import (
    ApplyCamera, Connect, Disconnect, HomeZero, MoveTo, RunSequence, Shutdown,
    TestExposure,
)
from fifa_gui.core.imageio import decimate, full_scale
from fifa_gui.hardware.base import (
    Aborted, DeviceError, NotConnectedError, NullCamera, NullStage,
)
from fifa_gui.hardware.discovery import probe

logger = logging.getLogger(__name__)

POLL_MS = 50
POSITION_EMIT_S = 0.2
PREVIEW_DECIMATION = 8

STATE_IDLE = "IDLE"
STATE_RUNNING = "RUNNING"
STATE_WAITING_OPERATOR = "WAITING_OPERATOR"
STATE_DONE = "DONE"
STATE_ABORTED = "ABORTED"
STATE_FAILED = "FAILED"


class _SaveSignals(QObject):
    saved = Signal(str, int)
    failed = Signal(str, str)


class _SaveTask(QRunnable):
    """Write a 93 MB frame without stalling the next stage move.

    Writes to <name>.npy.part and renames, so a crash never leaves a
    half-written frame that later looks like real data.
    """

    def __init__(self, path, array, signals):
        super().__init__()
        self.path = path
        self.array = array
        self.signals = signals

    def run(self):
        tmp = self.path + ".part"
        try:
            # Write through an open handle: np.save(path, ...) appends ".npy"
            # when the name does not already end in it, which would turn
            # "285000.npy.part" into "285000.npy.part.npy" and break the rename.
            with open(tmp, "wb") as fh:
                np.save(fh, self.array)
            os.replace(tmp, self.path)
            self.signals.saved.emit(self.path, os.path.getsize(self.path))
        except Exception as exc:
            logger.exception("saving %s failed", self.path)
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.signals.failed.emit(self.path, str(exc))


class AcquisitionWorker(QObject):
    hardware_changed = Signal(object, object, list)   # camera info, stage info, problems
    exposure_range = Signal(object)                  # (min_us, max_us, default_us) or None
    position_changed = Signal(int)
    limits_changed = Signal(int, int)
    temperature = Signal(object, object)
    state_changed = Signal(str, dict)
    frame_progress = Signal(int, int, str)
    frame_ready = Signal(object, dict)                # decimated preview, metadata
    frame_saved = Signal(str, int)
    log = Signal(int, str)
    sequence_finished = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self._q = queue.Queue()
        self._abort = threading.Event()
        self._continue = threading.Event()
        self._camera = NullCamera()
        self._stage = NullStage()
        self._save_signals = _SaveSignals()
        self._save_signals.saved.connect(self.frame_saved)
        self._save_signals.failed.connect(
            lambda p, m: self.log.emit(logging.ERROR, f"save failed: {p}: {m}")
        )
        self._savers = QThreadPool()
        self._savers.setMaxThreadCount(1)   # sequential writes, but overlapped with moves
        self._index = 0
        self._total = 0
        self._running = False

    # -------------------------------------------- called from the GUI thread

    def post(self, command):
        self._q.put(command)

    def request_abort(self):
        self._abort.set()

    def operator_continue(self):
        self._continue.set()

    @property
    def busy(self):
        return self._running

    # ------------------------------------------------ runs in the worker thread

    @Slot()
    def run(self):
        while True:
            try:
                command = self._q.get(timeout=1.0)
            except queue.Empty:
                self._housekeeping()
                continue

            if isinstance(command, Shutdown):
                self._close_devices()
                return
            try:
                self._dispatch(command)
            except Aborted as exc:
                self.log.emit(logging.WARNING, str(exc))
            except (DeviceError, NotConnectedError) as exc:
                self.log.emit(logging.ERROR, str(exc))
                self.state_changed.emit(STATE_FAILED, {"detail": str(exc)})
            except Exception as exc:                       # never let the thread die
                logger.exception("command failed: %r", command)
                self.log.emit(logging.ERROR, f"{type(exc).__name__}: {exc}")
                self.state_changed.emit(STATE_FAILED, {"detail": str(exc)})

    def _dispatch(self, command):
        if isinstance(command, Connect):
            self._connect(command)
        elif isinstance(command, Disconnect):
            self._close_devices()
            self.hardware_changed.emit(None, None, [])
        elif isinstance(command, MoveTo):
            self._move_and_wait(command.position)
        elif isinstance(command, HomeZero):
            self.log.emit(logging.INFO, "homing ...")
            self._stage.home_zero()
            self._wait_for_stop()
            self.log.emit(logging.INFO, f"homed, position {self._stage.position()}")
        elif isinstance(command, ApplyCamera):
            applied = self._camera.configure(command.settings)
            self.log.emit(logging.INFO, f"camera set to {applied}")
        elif isinstance(command, TestExposure):
            self._test_exposure(command.settings)
        elif isinstance(command, RunSequence):
            self._run_sequence(command.plan)

    def _housekeeping(self):
        """Runs between commands: cooler readout and a live position."""
        if self._camera.connected:
            try:
                self.temperature.emit(*self._camera.temperature())
            except Exception:
                pass
        if self._stage.connected and not self._running:
            try:
                self.position_changed.emit(self._stage.position())
            except Exception:
                pass

    # ------------------------------------------------------------- devices

    def _connect(self, command):
        self._close_devices()
        self.log.emit(logging.INFO, "looking for the camera and stage ...")
        result = probe(
            dll_path=command.dll_path or None,
            stage_uri=command.stage_uri or None,
        )
        self._camera = result.camera
        self._stage = result.stage

        camera_info = self._camera.info() if self._camera.connected else None
        stage_info = self._stage.info() if self._stage.connected else None
        self.hardware_changed.emit(camera_info, stage_info, result.problems)

        if self._stage.connected:
            left, right = self._stage.limits()
            self.limits_changed.emit(left, right)
            self.position_changed.emit(self._stage.position())
            self.log.emit(logging.INFO, f"stage {stage_info.uri}, limits {left} .. {right}")
        if self._camera.connected:
            self.log.emit(logging.INFO, f"camera {camera_info.name} ({camera_info.detail})")
            rng = getattr(self._camera, "exposure_range_us", lambda: None)()
            self.exposure_range.emit(rng)
            if rng:
                self.log.emit(
                    logging.INFO,
                    f"exposure range {rng[0]:,} .. {rng[1]:,} us (default {rng[2]:,})",
                )
        for device, message, _ in result.problems:
            self.log.emit(logging.WARNING, f"{device}: {message}")

    def _close_devices(self):
        for device in (self._camera, self._stage):
            try:
                device.close()
            except Exception:
                logger.exception("closing %r failed", device)
        self._camera = NullCamera()
        self._stage = NullStage()

    # -------------------------------------------------------------- motion

    def _check_abort(self):
        if self._abort.is_set():
            self._stage.stop()
            raise Aborted("aborted by the operator")

    def _wait_for_stop(self):
        """Poll instead of command_wait_for_stop, which blocks uninterruptibly."""
        last = 0.0
        while True:
            self._check_abort()
            status = self._stage.status()
            now = time.monotonic()
            if now - last > POSITION_EMIT_S:
                self.position_changed.emit(status.position)
                last = now
            if not status.moving:
                break
            QThread.msleep(POLL_MS)

    def _move_and_wait(self, target, settle_ms=0):
        self._stage.move_to(int(target))
        self._wait_for_stop()
        if settle_ms:
            QThread.msleep(int(settle_ms))
        position = self._stage.position()
        self.position_changed.emit(position)
        return position

    def _await_operator(self, position):
        """Replaces the notebook's input() prompt; stays abortable."""
        self._continue.clear()
        self.state_changed.emit(STATE_WAITING_OPERATOR, {"position": position})
        while not self._continue.wait(timeout=0.2):
            if self._abort.is_set():
                raise Aborted("aborted while waiting for the operator")
        self.state_changed.emit(STATE_RUNNING, {})

    # ------------------------------------------------------------ exposure

    def _frame_metadata(self, image, position=None):
        peak = float(image.max())
        scale = full_scale(image.dtype)
        return {
            "peak": peak,
            "saturated": peak >= 0.98 * scale,
            "full_scale": scale,
            # The ASI294MM Pro is a 12-bit sensor read out as RAW16 (value << 4),
            # so the useful number to show an operator is the 12-bit one.
            "peak_native": peak / 16.0 if image.dtype == np.uint16 else peak,
            "position": position,
            "shape": image.shape,
        }

    def _expose(self, timeout_s):
        # One capture() call, never start_exposure() first: zwoasi's capture()
        # starts the exposure itself, so the notebook exposed every frame twice.
        return self._camera.capture(timeout_s=timeout_s, abort=self._abort)

    def _test_exposure(self, settings):
        self._camera.configure(settings)
        self.log.emit(logging.INFO, "test exposure ...")
        image = self._expose(max(settings.exposure_s * 4 + 30, 30))
        meta = self._frame_metadata(image)
        self.frame_ready.emit(decimate(image, PREVIEW_DECIMATION), meta)
        self.log.emit(
            logging.INFO,
            f"test exposure peak {meta['peak']:.0f} ADU "
            f"({meta['peak_native']:.0f}/4095)"
            + ("  SATURATED" if meta["saturated"] else ""),
        )

    # ------------------------------------------------------------ sequence

    def _run_sequence(self, plan):
        self._abort.clear()
        self._running = True
        self._index = 0
        self._total = plan.frame_count
        started = datetime.now()
        record = {
            "app": "FIFA Calibrate",
            "version": __version__,
            "started": started.isoformat(timespec="seconds"),
            "plan": plan.to_dict(),
            "frames": [],
        }

        try:
            applied = self._camera.configure(plan.camera)
            self.log.emit(logging.INFO, f"camera set to {applied}")
            record["camera_applied"] = applied
            self.state_changed.emit(STATE_RUNNING, {})

            # Always home first, so every run starts from the same datum and the
            # positions in the file names mean the same thing across runs.
            self._check_abort()
            self.frame_progress.emit(0, self._total, "homing")
            self.log.emit(logging.INFO, "homing before the sequence ...")
            self._stage.home_zero()
            self._wait_for_stop()
            homed_at = self._stage.position()
            self.position_changed.emit(homed_at)
            self.log.emit(logging.INFO, f"homed, position {homed_at}")
            record["homed_before_sequence"] = True
            record["position_after_home"] = homed_at

            for target in plan.targets:
                self._check_abort()

                self.frame_progress.emit(self._index, self._total, f"moving to {target}")
                achieved = self._move_and_wait(target, plan.settle_ms)
                self.log.emit(logging.INFO, f"at {achieved} (target {target})")

                if plan.pause_for_operator:
                    self._await_operator(achieved)

                self.frame_progress.emit(
                    self._index, self._total,
                    f"retracting to {achieved - plan.backoff}",
                )
                self._move_and_wait(achieved - plan.backoff, plan.settle_ms)

                for k in range(max(plan.repeats, 1)):
                    self._check_abort()
                    self._index += 1
                    self.frame_progress.emit(self._index, self._total, "exposing")
                    image = self._expose(max(plan.camera.exposure_s * 4 + 60, 60))

                    meta = self._frame_metadata(image, achieved)
                    name = plan.filename(achieved, k)
                    path = os.path.join(plan.out_dir, name)

                    self.frame_progress.emit(self._index, self._total, f"saving {name}")
                    self._savers.start(_SaveTask(path, image, self._save_signals))
                    self.frame_ready.emit(decimate(image, PREVIEW_DECIMATION), meta)

                    # Saturation is recorded and reported, and the run continues.
                    self.log.emit(
                        logging.WARNING if meta["saturated"] else logging.INFO,
                        f"{name}: peak {meta['peak']:.0f} ADU "
                        f"({meta['peak_native']:.0f}/4095)"
                        + ("  SATURATED" if meta["saturated"] else ""),
                    )
                    record["frames"].append({
                        "file": name,
                        "target": target,
                        "achieved": achieved,
                        "repeat": k,
                        "peak_adu": meta["peak"],
                        "saturated": meta["saturated"],
                        "time": datetime.now().isoformat(timespec="seconds"),
                    })

            self._savers.waitForDone()
            record["finished"] = datetime.now().isoformat(timespec="seconds")
            record["outcome"] = "complete"
            self._write_run_record(plan, record)
            self.state_changed.emit(STATE_DONE, {})
            self.sequence_finished.emit(
                True, f"{len(record['frames'])} frames saved to {plan.out_dir}"
            )

        except Aborted as exc:
            self._savers.waitForDone()
            record["outcome"] = "aborted"
            record["finished"] = datetime.now().isoformat(timespec="seconds")
            self._write_run_record(plan, record)
            self.state_changed.emit(STATE_ABORTED, {"detail": str(exc)})
            self.sequence_finished.emit(False, str(exc))

        except Exception as exc:
            logger.exception("sequence failed")
            self._savers.waitForDone()
            record["outcome"] = f"failed: {exc}"
            self._write_run_record(plan, record)
            self.state_changed.emit(STATE_FAILED, {"detail": str(exc)})
            self.sequence_finished.emit(False, f"{type(exc).__name__}: {exc}")

        finally:
            self._running = False

    def _write_run_record(self, plan, record):
        """Provenance next to the data -- what the notebook never recorded."""
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(plan.out_dir, f"run_{stamp}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2)
            self.log.emit(logging.INFO, f"run record written to {os.path.basename(path)}")
        except OSError as exc:
            self.log.emit(logging.WARNING, f"could not write the run record: {exc}")
