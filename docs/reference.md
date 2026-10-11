# FIFA GUI — reference

Everything beyond the three steps in the [README](../README.md): what the
installer does, the drivers it cannot install for you, how to build the `.exe`
files, how to run the tests, and what was corrected while porting
`lab/fifa_calibration.ipynb`.

Both apps ship as standalone `.exe` bundles. Calculate needs no hardware and
runs on any Windows PC; only Calibrate talks to the ZWO ASI294MM Pro and the
Standa/XIMC stage.

---

## Install

```
.\install.bat                 analysis only (Calculate)
.\install.bat -Hardware       lab PC: adds the camera and stage packages
.\install.bat -Build          build machine: adds PyInstaller and pytest
```

Double-clicking `install.bat` works: it passes `-ExecutionPolicy Bypass` for
that one process, so no admin rights or policy changes are needed.

The script finds a 64-bit Python 3.12, creates `.venv`, installs the pinned
dependencies, fetches `ASICamera2.dll` (with `-Hardware`/`-Build`), and verifies
the result — including constructing a real `QApplication` (importing PySide6 is
not the same as having a working Qt platform plugin) and checking for
`msvcr120.dll`.

### ASICamera2.dll

ZWO's SDK binary is **not kept in this repository**, so it is fetched once per
machine:

```
.\tools\fetch_asi_dll.ps1                         find it, or download it
.\tools\fetch_asi_dll.ps1 -From <zip|dll|folder>  use a copy you already have
.\tools\fetch_asi_dll.ps1 -NoDownload             never download; just explain
```

In order it keeps an existing `vendor\zwo\ASICamera2.dll`, takes one from
`-From`, looks for an SDK already on the machine, and finally downloads the ASI
Camera SDK from ZWO. The download is ~110 MB because ZWO ships the Android,
Linux and Windows SDKs in one archive; only the 2.9 MB Windows **x64** DLL is
kept, and a 32-bit one is rejected outright — loading it gives an unhelpful
"not a valid Win32 application".

Only the Calibrate app needs it. Calculate builds and runs without it.

### Steps pip cannot do, on the lab PC only

1. **ZWO ASI camera USB driver** — from
   <https://www.zwoastro.com/downloads/developers>. Separate from
   `ASICamera2.dll`, which the installer fetches. Use a direct USB3 port, not a
   hub: a full RAW16 frame is 93 MB over the bus.
2. **Standa / XIMC USB driver** — from <https://files.xisupport.com/>,
   `ximc-2.x.x.zip` → `ximc\win64\`. Confirm it works in Standa's XILab first.
3. **MSVC++ 2013 x64 redistributable** — <https://aka.ms/highdpimfc2013x64enu>.
   **Required by the stage**: `libximc` probes for `msvcr120.dll` when imported
   and refuses to load without it.
4. **MSVC++ 2015–2022 x64** —
   <https://aka.ms/vs/17/release/vc_redist.x64.exe>, for CPython, Qt and numpy
   on a fresh image.

## Run from source

Double-clicking `Run FIFA Calculate.bat` or `Run FIFA Calibrate.bat` is the
normal way. They just wrap:

```
.venv\Scripts\python -m fifa_gui.apps.calculate_main [dataset_dir ...]
.venv\Scripts\python -m fifa_gui.apps.calibrate_main [--no-connect]
```

Arguments passed to the `.bat` are forwarded, so
`Run FIFA Calibrate.bat --no-connect` works. `--no-connect` opens Calibrate
without touching the camera or stage, so it can be started while someone else
is using the bench.

## Build the executables

```
.\build.ps1                   both, plus distributable zips
.\build.ps1 -Only Calculate
.\build.ps1 -DebugConsole     console window, to see a frozen crash's traceback
```

Output lands in `dist\`. Both are **onedir** bundles: a onefile build would
re-extract ~150 MB to `%TEMP%` on every launch and trips antivirus heuristics
far more often.

| | folder | zip |
|---|---|---|
| FIFA Calculate | 166 MB | 72 MB |
| FIFA Calibrate | 121 MB | 49 MB |

Building Calibrate needs `vendor\zwo\ASICamera2.dll` present; `build.ps1` stops
with a pointer to `tools\fetch_asi_dll.ps1` if it is not. In the finished bundle
the DLL sits **next to `FIFA Calibrate.exe`**, so a newer SDK can be dropped in by
overwriting that one file; a copy under `_internal\vendor\zwo\` is the fallback.

### Distributing

Extract the **whole folder** before running the exe — launching it from inside
Explorer's zip preview fails, because `_internal\` is left behind. On a machine
that received the zip by email or download:

```powershell
Expand-Archive .\FIFA-Calculate-0.1.0-win64.zip -DestinationPath C:\Apps
Get-ChildItem C:\Apps -Recurse | Unblock-File
```

The first ever launch may take an extra 5–20 s while matplotlib builds its font
cache. That is not a hang.

---

## Using Calibrate

1. Connect, then **Home** if the stage has not been homed this session.
2. Set the output directory, the position list, and the back-off (default
   110000 — how far to retract so the obscuration clears the beam).
3. Set gain and **exposure**. The notebook never set exposure at all, which is
   why the archived `mast00` and `mast03` datasets are saturated; use **Test
   exposure** and watch the peak ADU readout.
4. **START**. At each position the app moves, pauses so you can recentre the
   spot, retracts by the back-off, exposes and saves.
5. **CONTINUE** resumes after each pause; **ABORT** stops at any point.

Each frame is named after the position the stage **actually reached**, which is
why a 285000 target produces `284999.npy`. Alongside the data the run leaves
`run_<timestamp>.json` with the plan, the camera settings, the achieved
positions and the per-frame peak ADU.

Saturated frames are logged and kept — Calculate flags them for exclusion.

## Using Calculate

1. **Add dataset directory** — any folder of frames named `<position>.npy`
   (also `.fit`, `.jpg`, `.png`, `.tif`), optionally `<position>_<repeat>.npy`.
2. The **Track** tab shows the frame table, the fit, and the residuals. Untick a
   frame to drop it from the fit; the fit and both plots update immediately.
3. To fix a bad centroid, tick **Click to re-centroid** and click the real spot.
   `np.argmax` over a 46 MP frame is fragile — a cosmic ray or a reflection can
   steal it. **Revert to automatic** undoes it.
4. **Inverse** solves `col(w) = x` for the stage position, per dataset, and
   averages across them.
5. **Flexure** scatters repeated measurements at one position against an
   acceptance circle given as a **diameter**.
6. **Export results** writes the per-frame CSV, `fits.json`, `inverse.csv`,
   `session.json` and the three plots as PNG. `session.json` restores the
   include flags and manual overrides via **File → Open session** — centroid QA
   is human judgement, and it would otherwise be lost when the window closes.
7. **Units tab** keeps a running table of fit coefficients per unit
   (`results/units.json`, tracked in git). Select a dataset, name the unit
   (guessed from `.../mast03/1`) and press **Update from selected dataset**;
   the unit's row is replaced. The same tab interpolates every unit at once:
   stage + y from a target x, or x + y from a stage position.

### Conventions and corrections

The notebook's reporting convention is preserved: **`x` is the image column and
`y` is the image row**. Four defects were corrected in the port:

- **The centroid axis swap.** `photutils.centroid_com` returns `(column, row)`
  but the notebook unpacked it as `(row, column)`, so the sub-pixel refinement
  landed on the wrong axis. Fixing it moves the reported fiber position by
  ~1.0 px on `mast04/1` and ~0.1 px on `mast03/1`.
- **The flexure circle.** `plt.Circle(centre, 30/2.35)` takes a **radius**, so
  the notebook drew a 60 µm circle while labelling it 30 µm. The field is now a
  diameter; enter 60 to reproduce the old plot.
- **Flexure filenames.** `int("324998_0")` is `3249980` in Python — underscores
  are digit separators — so the notebook silently read those positions 10×
  wrong.
- **Double exposure.** The notebook called `start_exposure()` and then
  `capture()`, which starts another one.

## Tests

```
.venv\Scripts\python -m pytest tests -q
```

The GUI tests run headless on Qt's offscreen platform and against small
synthetic frames; nothing in the suite opens the real camera or stage. The
golden values in `tests/test_core.py` are checked against the real `mast03/1`
and `mast04/1` data when it is present, and skip when it is not.
