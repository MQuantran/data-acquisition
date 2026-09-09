# Releasing data_acquisition

The app is a normal Windows desktop program. Users download a zip from the
project's **GitHub Releases** page, extract it, and run `DataAcquisition.exe`.
`Help ▸ Check for updates…` inside the app reads that same Releases page and
tells them when a newer version is out.

This file is for the maintainer.

---

## One-time GitHub setup

1. **Create the repository** (free) at <https://github.com/new>:
   - Owner: `MQuantran` · Name: **`data-acquisition`** · Public.
   - The name **must match `REPO` in `updater.py`**
     (`MQuantran/data-acquisition`). If you use a different owner/name, change
     that one line (or set the env var `DAQ_UPDATE_REPO=owner/name` when
     running).
   - Don't add a README/licence from the wizard — this folder already has them.

2. **Push this folder** (run from `D:/PhD/apps/data_acquisition`):

   ```
   git init                       # already done if this folder has a .git
   git add .
   git commit -m "data_acquisition 0.4.0"
   git branch -M main
   git remote add origin https://github.com/MQuantran/data-acquisition.git
   git push -u origin main
   ```

3. That's it. GitHub Actions (`.github/workflows/release.yml`) is already in the
   repo and needs no secrets — it uses the built-in token.

---

## Cutting a release

1. **Bump the version** — one place: `VERSION = "x.y.z"` in `app.py`.
   (`build.py` regenerates `VERSION` and `file_version_info.txt` from it.)
2. **Update `CHANGELOG.md`** — add a section for the new version at the top.
3. Commit:
   ```
   git add app.py CHANGELOG.md
   git commit -m "data_acquisition x.y.z"
   git push
   ```
4. **Tag and push the tag:**
   ```
   git tag vx.y.z
   git push origin vx.y.z
   ```
5. GitHub Actions builds `DataAcquisition-vx.y.z-win64.zip`, runs the
   self-tests, and publishes a Release with the zip attached. Watch it on the
   repo's **Actions** tab (~5 min).
6. Users' apps pick it up on their next **Check for updates** (or next startup).

### Version numbers

`MAJOR.MINOR.PATCH`. Bump PATCH for fixes, MINOR for features, MAJOR for
breaking changes. `updater.parse_version` compares numerically
(`0.10.0 > 0.9.0`) and ignores any `-suffix`.

---

## Building locally (no release)

```
python -m pip install -r requirements.txt
python build.py            # -> dist/DataAcquisition/  +  dist/DataAcquisition-v<VER>-win64.zip
python build.py --onefile  # single .exe instead of a folder
```

Hand the **zip** to someone directly if you don't want to cut a release.
One-folder is preferred (faster start, fewer antivirus false positives).

## If you ever move the repo

Change `REPO` in `updater.py`, release a new version, and tell users to update
once manually (old builds still point at the old location).

## Manual release (if Actions is unavailable)

`python build.py`, then on the repo's **Releases** page: *Draft a new release* →
choose the tag `vx.y.z` → drag in `dist/DataAcquisition-vx.y.z-win64.zip` →
*Publish*.
