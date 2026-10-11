# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Documentation layout

`README.md` is deliberately minimal — three steps to get running and nothing
else. **Do not grow it.** Installer options, driver prerequisites, build and
packaging detail, test notes and the notebook-defect list all belong in
`docs/reference.md`; architecture notes belong here.

## Project overview

Two PySide6 desktop applications for the FIFA fiber calibration at the Weizmann
MAST bench, ported from `C:\Users\oreni\Documents\code\lab\fifa_calibration.ipynb`
(which is deliberately left untouched — this project does not import from it).

- **Calibrate** drives a ZWO ASI294MM Pro (`zwoasi`) and a Standa/XIMC stage
  (`libximc`) through a sweep of positions, saving one 93 MB `.npy` per position.
- **Calculate** centroids those frames offline, fits the track, exports results.

## Do not open the bench hardware

The camera and the stage are shared and are often in use. **Never run device
enumeration, `probe()`, `open()` or any `command_move` to check something.**
Develop against the saved `.npy` data and the offscreen Qt platform; the
"no hardware detected" path is the normal development case. `calibrate_main.py
--no-connect` opens the app without touching the devices.

## Setup

```
.\install.bat -Build          creates .venv, installs pinned deps + PyInstaller
.venv\Scripts\python -m pytest tests -q
.\build.ps1                   builds both exes into dist\
```

`vendor\zwo\ASICamera2.dll` is ZWO's proprietary SDK binary. It is **gitignored,
not committed**. `tools\fetch_asi_dll.ps1` obtains it — existing copy, then
`-From`, then an SDK already on the machine, then a download from ZWO — and
`install.bat -Hardware` calls it. Do not re-add the DLL to the repository.
Only Calibrate needs it; Calculate builds and runs without it.

## Architecture

```
fifa_gui/
  core/        pure analysis: numpy only, no Qt, no hardware
  qtutil/      shared widgets (ImageView, MplPanel, LogPanel)
  hardware/    device interfaces + ZWO/XIMC implementations + null devices
  apps/        calibrate/ and calculate/ windows, plus the two entry points
```

`core` is the single implementation of the analysis and imports nothing but
numpy, so it stays usable from a notebook. Keep it free of Qt.

### Conventions that are easy to get wrong

- **`x` is the image column, `y` is the image row.** Inherited from the
  notebook. Internally everything is `(row, col)`; the swap happens only at the
  reporting boundary, in `core/linefit.py`.
- `core/centroid.py` computes its own centre of mass **in `(row, col)` order**.
  Do not reintroduce `photutils.centroid_com`, which returns `(column, row)` —
  unpacking it the other way round was the original ~1 px bug.
- Fits use `np.polyfit(..., cov=True)`, which matches `curve_fit` to ~1e-9 on
  this linear model and returns the covariance. **scipy is not a runtime
  dependency** and should not become one.
- Frame names are the position the stage **actually reached**, not the target,
  and may carry a `_<repeat>` suffix. Parse with `position_from_filename`, never
  `int(name.split('.')[0])`: `int("324998_0")` is `3249980`.
- `np.save(path, ...)` appends `.npy` when the name lacks it. The atomic save in
  `calibrate/worker.py` writes through an open handle for exactly this reason.

### Threading

- **Calibrate**: one worker `QObject` on a `QThread` owns both devices; the GUI
  thread never touches them. It consumes a `queue.Queue`, not queued slot calls
  — a queued slot is only delivered when the worker is idle, so an Abort posted
  during a 30 s exposure would sit unprocessed. Abort and Continue are
  `threading.Event`s, which *are* visible mid-operation. Stage moves poll
  `status()` rather than `command_wait_for_stop`, which blocks uninterruptibly
  inside the C library.
- **Calculate**: frames load on a `QThreadPool`; the 93 MB array is discarded
  after centroiding, keeping only a decimation pyramid and a 256² cutout.
  Re-fitting is microseconds and stays on the GUI thread.
- The re-fit debounce listens to the model's **`includeChanged`**, not
  `dataChanged`: a re-fit repaints the table, which emits `dataChanged`, which
  would restart the timer and spin for ever.

### Hardware

`import libximc` loads native DLLs **at import time** and raises without the
MSVC++ 2013 redistributable, so it must only ever be imported inside
`hardware/ximc.py::load_ximc()`. A module-level import would stop the frozen
app opening at all. `zwoasi` imports safely, but `asi.init()` needs an absolute
path and may only be called once per process — see `hardware/asi_loader.py`.

## Packaging

onedir, never onefile, and `upx=False`. The `libximc` DLLs are package *data*,
invisible to PyInstaller's analyser, and its loader resolves them relative to
its own `__file__` — so `collect_data_files` must place them at
`libximc/library-files/win64/`. `build.ps1` asserts they arrived.

PowerShell 5.1 turns native-command stderr into a terminating error under
`$ErrorActionPreference='Stop'`; `build.ps1` wraps such calls in
`Invoke-Native`. Also note `$args` is an automatic variable — do not use it as
a local name in a function.

## Testing

GUI tests run on `QT_QPA_PLATFORM=offscreen` against small synthetic frames
(`tests/conftest.py`) with a known ground-truth track, and against fake devices
(`tests/fakes.py`). Real-data golden values live in `tests/test_core.py` and
skip when `lab\` is absent:

| dataset | x(300000) | y(300000) |
|---|---|---|
| `lab\mast03\1` | 4569.71 | 2812.83 |
| `lab\mast04\1` | 4395.82 | 2536.49 |

Destroy windows in fixtures (`close()` + `deleteLater()` + `processEvents()`);
leaving them alive makes the suite slow down quadratically.
