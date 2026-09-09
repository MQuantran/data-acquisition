# Changelog

All notable changes to **data_acquisition**. Newest first.
Version scheme: `MAJOR.MINOR.PATCH`.

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
