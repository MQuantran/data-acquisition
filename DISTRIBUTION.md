# data_acquisition — install & distribution

A native desktop app (a normal window, **not** a browser page). It runs fully
offline; nothing you paste or export leaves the machine.

---

## For lab users — just run it

**Windows (no Python, no admin rights needed):**

1. Go to the **Releases** page:
   <https://github.com/MQuantran/data-acquisition/releases/latest>
   and download **`DataAcquisition-v<version>-win64.zip`** (under *Assets*).
2. **Right-click the zip → Extract All…** to a folder you can write to
   (e.g. `Documents\DataAcquisition`). Do *not* run it from inside the zip.
3. Open the extracted `DataAcquisition` folder and double-click
   **`DataAcquisition.exe`**.
4. First launch: Windows **SmartScreen** may say *"Windows protected your PC"*
   because the app is not code-signed. Click **More info → Run anyway**. This is
   a one-time prompt per machine.
5. The window opens. Use **Help → Quick guide** for the workflow. Your CSV/JSON
   goes wherever you choose in the **Export…** dialog.

First start takes a few seconds (it loads Python + OpenCV from the folder);
after that it is instant.

### Staying up to date

The app checks for a newer version on startup (needs internet) and under
**Help → Check for updates…**. When one is available it offers to open the
Releases page. To install it: download the new zip, extract it, and use that
folder instead of the old one. Your preferences (window size, update-check
toggle) live in `%APPDATA%\DataAcquisition\` and carry over.

No internet? The check quietly says so and the app works normally — digitizing
never needs a connection.

**macOS / Linux users:** no prebuilt bundle yet — run from source (below), or ask
the maintainer to run `python build.py` on that OS.

### If your antivirus quarantines it

Unsigned PyInstaller apps are sometimes flagged by heuristic scanners (there is
no actual malware — the build uses no UPX/packing specifically to reduce this).
Options: ask IT to whitelist the `DataAcquisition` folder, or run from source.

---

## Run from source (any OS)

```
python -m pip install -r requirements.txt
python app.py
```

Python 3.10+ with Tk (the standard python.org installer includes it).
Checks: `python core.py --selftest`, `python updater.py --selftest`, and
`python app.py --selftest` (all print `RESULT: PASS`).

---

## For the maintainer

**Cutting a release (GitHub) and one-time repo setup → see `RELEASING.md`.**
The short version: bump `VERSION` in `app.py`, update `CHANGELOG.md`, commit,
`git tag vX.Y.Z && git push origin vX.Y.Z` — GitHub Actions builds and
publishes the zip, and users' "Check for updates" sees it.

### Building locally without a release

```
python -m pip install -r requirements.txt      # includes pyinstaller
python build.py                                # one-folder + zip  (recommended)
python build.py --onefile                      # single .exe + zip  (alt)
```

Output in `dist/`:

| build | output | notes |
|---|---|---|
| default | `dist/DataAcquisition/` + `DataAcquisition-v<VER>-win64.zip` | hand out the **zip** |
| `--onefile` | `dist/DataAcquisition.exe` + `…-onefile.zip` | one file, slower start, higher AV-flag rate |

Prefer the **one-folder** build for the lab — faster startup, far fewer
antivirus problems on managed machines.

### Version numbers

Bump **only** `VERSION` in `app.py`. `build.py` regenerates `VERSION` and
`file_version_info.txt` from it. The zip name, window title, Help → About and
`--version` all follow.

### Optional: kill the SmartScreen warning

If the group has a code-signing certificate:

```
signtool sign /fd SHA256 /a /tr http://timestamp.digicert.com /td SHA256 ^
  dist\DataAcquisition\DataAcquisition.exe
```

Sign the `.exe` inside the folder *before* zipping. Reputation with SmartScreen
still builds over the first few downloads even when signed.

### What's in the build

- `app.py` (Tkinter GUI) + `core.py` (calibration / detection / export)
- bundled: Python 3.12, numpy, Pillow, OpenCV, Tcl/Tk, `assets/icon.ico`
- excluded to keep it lean: scipy, matplotlib, Qt (`DataAcquisition.spec`)
- size ≈ **180 MB unzipped, ~72 MB zipped** (v0.2.0, win64). OpenCV + numpy +
  Tcl/Tk dominate. To roughly halve it: `pip uninstall opencv-python && pip
  install opencv-python-headless`, then rebuild — the headless build drops
  OpenCV's own GUI code, which this app does not use.
