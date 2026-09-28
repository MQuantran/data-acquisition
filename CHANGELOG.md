# Changelog

All notable changes to **data_acquisition**. Newest first.
Version scheme: `MAJOR.MINOR.PATCH`.

## 0.5.0 — 2026-09-28

Much better on crowded scatter plots, and faster everywhere.

- **Overlapping circle markers are split.** With marker shape `circle`, the new
  *Split overlapping markers* option (on by default) finds each marker inside a
  merged blob from its visible arc of edge, instead of returning one point per
  blob. On a synthetic benchmark (14 plots, 886 markers): 57 % -> 95 % of
  markers found, 99 % precision, ~0.4 px position error. Handles hollow
  circles, edged markers, markers under another series, markers cut by the
  plot-area box, and `'o-'` plots (a line through the markers). Marker size is
  learned from the plot; override with *Marker radius px*.
- **Snap marker.** Click one clean marker (on it, beside it, or inside a hollow
  one): shape (circle / square / diamond / triangle), filled / hollow / edged,
  the colour to track and the marker size are read off it and set for you.
  Warns when the snapped marker looks like several merged markers.
- **Mark colour** for detected points: `auto` picks a colour that stands out
  from your markers and the background (red markers -> cyan marks), or choose
  red / black / cyan / magenta. Marks have a contrasting halo.
- **Faster:** zoom and pan now draw only the visible part of the image (~5 ms
  per wheel step at any zoom, was up to ~400 ms); detection ~3.5x faster;
  snap ~10x faster. Maximum zoom raised to 32x.
- **Add point** mode: click to add a point Detect missed; it snaps onto the
  picked curve colour when one is nearby.

## 0.4.0 — 2026-09-09

Makes it behave like an installed desktop app.

- **Check for updates** — `Help ▸ Check for updates…` asks the GitHub releases
  page whether a newer version exists and, if so, offers to open the download
  page. Also runs once, quietly, at startup (toggle in the Help menu). Needs
  internet only for this one feature; if offline it says so and moves on.
- Remembers its **window size and position** between runs.
- Preferences stored in `%APPDATA%\DataAcquisition\config.json`
  (`~/.config/DataAcquisition/` on macOS/Linux).
- `build.py` now regenerates `VERSION` and `file_version_info.txt` from the
  single `VERSION` string in `app.py` — a release is a one-line bump.
- New `updater.py` and `appconfig.py` (standard-library only, no new
  dependency); both have `--selftest`.

## 0.3.0

- Square **eraser** tool: drag an adjustable box to wipe clumps of stray
  detected points (e.g. an annotation the detector read as markers).

## 0.2.0

- Packaged for lab distribution: window icon, `--version`, Help menu,
  one-folder PyInstaller build via `build.py`, `DISTRIBUTION.md`.

## 0.1.0

- First working digitizer: paste image, magnifier-loupe axis calibration,
  colour-mask line / point detection, CSV / TSV / JSON / litdata-csv export.
