# FIFA GUI

Two Windows applications for the FIFA fiber calibration.

- **FIFA Calibrate** — drives the ZWO ASI camera and the Standa/XIMC stage
  through a sweep of positions, saving one frame per position.
- **FIFA Calculate** — centroids those frames, fits the fiber track against
  stage position, and exports the results.

## Getting started

1. **Clone it**

   ```
   git clone https://github.com/orenir49/fifa_gui.git
   ```

2. **Double-click `install.bat`**

3. **Double-click `Run FIFA Calculate.bat`** — or `Run FIFA Calibrate.bat`

The installer tells you if anything is missing that it could not install for
you.

> Don't want to clone? Download a ready-made `.zip` from
> [Releases](https://github.com/orenir49/fifa_gui/releases), extract the whole
> folder, and double-click the `.exe` inside.

## Using the apps

**Calibrate** — set the output directory and the list of positions, then press
START. At each position it moves, pauses for you to recentre the spot, retracts,
exposes and saves. CONTINUE resumes, ABORT stops.

**Calculate** — add a dataset directory. The Track tab shows the fit and the
residuals; untick a frame to drop it from the fit. Export writes the tables, the
plots and a session file you can reopen later.

## More

- [`docs/reference.md`](docs/reference.md) — installer options, drivers,
  building the `.exe` files, tests, and the defects corrected from the original
  notebook.
- [`CLAUDE.md`](CLAUDE.md) — architecture notes for working on the code.
