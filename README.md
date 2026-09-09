# data_acquisition — plot digitizer

A native desktop app (a normal window — **not** a browser page): paste a plot
image, calibrate the axes with a magnifier loupe, and pull the numbers back out
as CSV / TSV / JSON. Runs fully offline.

Born from the `onset_model/data/literature/` work — the `*.csv` it exports use a
header format compatible with `postprocessing/compare_lit_traces.py`
(`litdata-csv` option), so a digitized literature trace drops straight into that
comparison pipeline.

- **Lab users / distribution:** see **`DISTRIBUTION.md`** (download the zip,
  extract, run — no Python needed).
- **This file:** running from source + the design.

## Files

| file | what |
|---|---|
| `app.py` | the Tkinter GUI — **`VERSION` here is the single source of truth** |
| `core.py` | calibration + detection + export, no GUI — `python core.py --selftest` |
| `updater.py` | "Check for updates" — reads the GitHub Releases page, stdlib only — `python updater.py --selftest` |
| `appconfig.py` | per-user prefs (`%APPDATA%\DataAcquisition\config.json`): window size, update-check toggle |
| `assets/icon.ico` | app icon (regenerate: `python assets/make_icon.py`) |
| `requirements.txt` | deps (numpy, Pillow, opencv-python, scipy*, pyinstaller) |
| `build.py` | build + zip a distributable, and sync the version files — `python build.py [--onefile]` |
| `build_exe.bat` | Windows double-click wrapper around `build.py` |
| `DataAcquisition.spec` | PyInstaller one-folder spec (icon, version resource) |
| `VERSION`, `file_version_info.txt` | **auto-generated** from `app.py`'s `VERSION` by `build.py` — don't hand-edit |
| `.github/workflows/release.yml` | on a `v*` tag: build + publish a GitHub Release |
| `DISTRIBUTION.md` | install guide for lab users |
| `RELEASING.md` | maintainer: GitHub setup + how to cut a release |
| `CHANGELOG.md` | version history (shown in the update prompt) |

\* scipy is an optional fallback for point detection; opencv (always bundled in
the build) is the primary path.

## Run from source

```
python -m pip install -r requirements.txt
python app.py
```

Python 3.10+ with Tk. Headless checks: `python core.py --selftest`,
`python updater.py --selftest`, and `python app.py --selftest` (all print
`RESULT: PASS`). `python app.py --version` prints the version.

## Updates

`Help ▸ Check for updates…` (and one quiet check at startup — toggle in the Help
menu) asks the project's GitHub Releases page whether a newer version is
published; if so it offers to open the download page. That is the **only**
feature that touches the network — digitizing is fully offline. Offline, it just
says it couldn't reach GitHub and carries on.

To ship a new version, see **`RELEASING.md`** (bump `VERSION` in `app.py`, tag
`vX.Y.Z`, push — GitHub Actions builds and publishes the release).

## Workflow

1. **Image** — `Paste (Ctrl+V)` from the clipboard, or `Open…` a file.
   Type a **dataset name**.
2. **Output** — pick the **export format** and the **extract mode**:
   - **Line** — one sample per pixel column (median row of the colour mask).
   - **Points** — connected-component centroids; choose the **marker shape**
     (`any` / `circle` / `square` / `triangle` / `diamond` — a soft filter).
3. **Curve colour** — `Pick curve colour`, then click the curve / a marker.
   Adjust **Colour tolerance** if the mask is too tight or too greedy.
   Optionally `Set plot area (drag)` a rectangle around the axes box — this
   excludes the legend, tick labels and frame and sharply improves results.
4. **Calibration** — `Add calibration ref`, then:
   - click the **X reference** (e.g. an x-axis tick you know the value of),
   - click the **Y reference** (e.g. a y-axis tick),
   - type the two data values.

   Do this **twice** (4 clicks, 2 references). Tick **X log** / **Y log** for log
   axes. While aiming: **mouse-wheel** zooms about the cursor, **middle-drag**
   pans, and the **magnifier** (bottom-left, 5×) shows the exact pixel under a
   green crosshair with a red centre box. Once calibrated, the readout shows live
   `x, y` data coordinates under the cursor.
5. **Detect** — review the **red overlay**. Prune strays with either:
   - **Delete** — click a single red point (e.g. one mis-picked legend marker).
   - **Eraser** — tick it, set **Eraser size (px)**, then click/drag the red
     square to wipe every point inside it at once. Use this when the detector
     read an **annotation / text block** as a cloud of points.
6. **Export…** — writes the rows sorted by x, with a metadata header
   (dataset, source image, calibration references, axis types, timestamp).

## Accuracy notes

- Best on clean, high-DPI figures with distinct curve colours and linear axes:
  ~0.5–1 % of full scale, limited by line width and your calibration clicks.
- **Log axes**: calibrate in log space (tick the box) — a 1-pixel slip near the
  top of a decade is a big relative error.
- **Overlapping curves**: the colour mask can't separate them where they cross —
  digitize each colour separately, or prune in Delete mode.
- **Scanned / B&W figures**: pick a near-black colour with a wide tolerance and a
  tight plot-area rectangle; expect to prune more.
- Line mode smears near-vertical segments (median-of-column) — use Points there,
  or a finer x-step.
- Output is *digitized*, not data of record — the header keeps the provenance;
  don't fit smoothing curves to it.

## Build a distributable

```
python -m pip install -r requirements.txt
python build.py                # -> dist/DataAcquisition/  + DataAcquisition-v<VER>-win64.zip
python build.py --onefile      # -> single dist/DataAcquisition.exe  + zip
```

Hand the **zip** to lab users; they extract and run `DataAcquisition.exe` — see
`DISTRIBUTION.md`. One-folder is the default and preferred (faster start, far
fewer antivirus false positives on managed machines; UPX is disabled on
purpose). Full details, version-bump steps, and optional code-signing are in
`DISTRIBUTION.md`.

## Roadmap (post-MVP)

- Add-point mode (click on the curve → snap y from the mask).
- ≥2 calibration refs → least-squares axis fit + a residual readout.
- Rectangular-to-skewed axis support (projective transform) for photographed
  figures.
- Multi-series in one pass (name + colour per series).
- Save/load a `.daqproj` so a digitization can be revisited.
- Vector-PDF path extraction (exact) when the figure isn't raster.
- One-click *apply* update (download + swap the folder), not just "check".
- Windows installer (Start-menu entry / uninstaller) instead of extract-and-run.

See `PRODUCTIZATION_PLAN.md` for the fuller "make it a real distributed app"
roadmap.
