# data_acquisition — plan to turn it into a "real app" (installer + auto-update)

**Context.** The MVP is a working native Tkinter digitizer, packaged as a
one-folder PyInstaller zip that lab users extract and run
(`DISTRIBUTION.md`). Joel has seen it and wants it to become a proper
distributed application — installed like normal software, with versioned
releases and an update mechanism ("like the League client"). This document is
the roadmap for that.

**"Like League" translated to concrete pieces:**

| League client behaviour | What we actually need |
|---|---|
| You install it once, it's in the Start menu | A **Windows installer** (Inno Setup) — per-user, no admin, uninstaller |
| It checks for patches on launch and updates itself | An **in-app updater** that reads a release manifest and applies new versions |
| Patch notes / version number visible | **Semver + CHANGELOG + "What's new"** dialog, single source of truth for the version |
| It's a trusted signed binary (no scary warnings) | **Code signing** (Azure Trusted Signing) — optional but high value |
| Riot builds and ships patches on a schedule | **CI/CD** (GitHub Actions) that builds + publishes a release on every version tag |

---

## Current state vs. target

| Area | Now | Target |
|---|---|---|
| Source control | **none** — `apps/data_acquisition/` is a loose folder, not in any git repo | Own GitHub repo (or monorepo with the other `apps/`) |
| Versioning | `VERSION = "0.3.0"` hardcoded in `app.py`; `VERSION` file exists but is **empty**; `file_version_info.txt` hand-synced | One `__version__.py`; everything else generated from it |
| Build | `python build.py` locally → zip | GitHub Actions on `v*` tag → signed installer + portable zip + manifest, attached to a GitHub Release |
| Distribution | email / shared drive a zip | Stable Releases page; installer; (later) silent auto-update |
| Update | "delete old folder, extract new zip" | Launch-time check → notify → one-click apply |
| Trust | unsigned; SmartScreen "Run anyway" every new machine | Signed; no prompt |
| Config / logs | none (nothing persisted) | `%APPDATA%\DataAcquisition\` — config.json, rotating logs, crash reports |
| Project files | export-only, one-shot | `.daqproj` save/reload (image + calibration + points), file association |

---

## Recommended stack

- **Repo:** new **public** GitHub repo `data-acquisition` under `MQuantran`.
  Public is the pragmatic choice — nothing in a plot digitizer is sensitive,
  and a public repo means the updater needs **no embedded credentials** (the
  GitHub Releases API and asset downloads are anonymous). If it must be private,
  the updater needs a bundled fine-grained read-only PAT or a tiny redirect
  proxy — avoid unless required.
- **Version single source of truth:** `data_acquisition/__version__.py`
  (`__version__ = "0.3.0"`). `app.py` imports it; `build.py` **generates**
  `file_version_info.txt` from it; CI reads it to name the release. Kills the
  three-places-to-edit problem and the empty `VERSION` file.
- **Packaging:** keep PyInstaller **one-folder** (faster start, far fewer AV
  hits than one-file on managed machines — already the house choice).
- **Installer:** **Inno Setup**. Free, battle-tested, scriptable. Per-user
  install to `%LOCALAPPDATA%\Programs\DataAcquisition` (no admin), Start-menu
  shortcut, uninstaller, "launch on finish", optional desktop icon, writes the
  install path to `HKCU` so the updater can find it.
- **Updater:** stage it (see phases). Start with a **home-rolled GitHub-Releases
  check**; graduate to **[tufup](https://github.com/dennisvang/tufup)** if the
  release cadence picks up — it's built specifically for PyInstaller apps, does
  **signed delta patches** (only ships changed files — important when the bundle
  is ~70 MB zipped), and applies atomically on restart. tufup gets us ~90 % of
  the "launcher/patcher" experience without a separate launcher process.
- **Signing:** **Azure Trusted Signing** (~USD $10/month, Microsoft-operated, no
  hardware token, runs in GitHub Actions). This is the single biggest "is this a
  real app" signal for the lab — it removes the SmartScreen wall. Fallback:
  stay unsigned and keep the `DISTRIBUTION.md` "More info → Run anyway" note.
- **CI:** GitHub Actions, `windows-latest`, trigger on `v*` tags.
- **Crash/logging:** `logging` to a rotating file in `%APPDATA%\DataAcquisition\logs\`;
  top-level `try/except` in `main()` that logs the traceback and shows a
  "copy this and send to Minh" dialog. Sentry is optional and probably overkill
  for a lab tool.

---

## Phased plan

### Phase 0 — foundations  (~½ day, $0)
- [ ] Create the GitHub repo; move `apps/data_acquisition/` in (fresh history is
      fine — the folder has no git history to preserve).
- [ ] `data_acquisition/__version__.py` as the only version string; import it in
      `app.py`; have `build.py` emit `file_version_info.txt` from it.
- [ ] `LICENSE` (MIT), `CHANGELOG.md` (Keep a Changelog format), `pyproject.toml`
      (metadata, pinned deps, `ruff` config).
- [ ] Tag `v0.3.0` as the baseline.
- **Outcome:** a normal project other people can clone, read, and trust.

### Phase 1 — CI build + GitHub Releases  (~1 day, $0)
- [ ] Actions workflow: on `v*` tag → `pip install` → `core.py --selftest` +
      `app.py --selftest` → `python build.py` → `gh release create` with the
      portable zip.
- [ ] Emit a **`latest.json`** manifest as a release asset:
      `{ "version", "url", "sha256", "notes", "min_supported", "published" }`.
- **Outcome:** releasing = `git tag v0.4.0 && git push --tags`. Lab users
  download from a stable Releases page instead of a drive folder.

### Phase 2 — in-app update check, notify-only  (~1 day, $0)   ← ship this to Joel
- [ ] On startup, background thread (2 s timeout, fail-silent): fetch
      `latest.json`; if newer than `__version__`, show a dismissible banner /
      toolbar button: **"v0.4.0 available — What's new / Download"**.
- [ ] `Help → Check for updates…` for a manual trigger.
- [ ] `%APPDATA%\DataAcquisition\config.json`: `update_check` on/off, `channel`
      (stable / beta).
- **Outcome:** the app is now version-aware and tells you when it's stale — it
  already *feels* like a real app even though the install step is still manual.

### Phase 3 — Windows installer  (~1–2 days, $0)
- [ ] Inno Setup script (`installer/DataAcquisition.iss`): per-user, Start-menu,
      uninstaller, launch-on-finish, writes install path to `HKCU`.
- [ ] CI builds `DataAcquisitionSetup.exe` alongside the portable zip; both
      attached to the release.
- [ ] Split docs: `INSTALL.md` (users — "download the setup, next-next-finish")
      vs `RELEASING.md` (maintainer).
- **Outcome:** installs and uninstalls like any Windows program.

### Phase 4 — apply updates from inside the app  (~2–3 days, $0)  ← the "League" bit
- **Option A (pragmatic, do first):** "Update now" → download the new
  `...Setup.exe` to `%TEMP%` → verify sha256 → run it `/SILENT /CLOSEAPPLICATIONS`
  → app exits → installer swaps files and relaunches.
- **Option B (proper, do if cadence increases):** adopt **tufup** — one-time TUF
  repo + signing-key setup, client pulls only changed files, atomic apply on
  restart. Best long-term given the bundle size.
- [ ] "What's new" dialog on first run after an update (renders `CHANGELOG.md`).
- **Outcome:** user clicks one button, app updates itself and restarts.

### Phase 5 — trust & polish  (ongoing)
- [ ] **Code signing** in CI (Azure Trusted Signing) — removes SmartScreen
      friction. ~$10/mo.
- [ ] Crash handler → log file + friendly dialog.
- [ ] `.daqproj` project files (save/reload a full digitizing session) +
      installer-registered file association — this is what makes it a *tool*,
      not a one-shot utility.
- [ ] Optional adoption metric: a single opt-in "opened, version, OS" ping to a
      Google Form/Sheet. Conservative; off by default.

### Phase 6 — cross-platform & scale  (only if needed)
- [ ] macOS `.app` + notarization (needs an Apple Developer account, $99/yr) and
      a Linux AppImage — only if lab members are off Windows.
- [ ] CI runs `core.py` tests on every push, not just tags.
- [ ] Issue templates, `CONTRIBUTING.md`, README GIF/screencast.
- [ ] Pick a searchable product name before any public push ("data acquisition"
      collides with NI-DAQ etc.).

---

## Effort & cost summary

| Milestone | Effort | Cost |
|---|---|---|
| Phases 0–2 (versioned, self-aware, GitHub Releases, notify-on-update) | ~3 days | $0 |
| Phase 3 (installer) | +1–2 days | $0 |
| Phase 4A (installer-swap auto-update) | +2–3 days | $0 |
| Phase 4B (tufup delta updates) | +setup | $0 |
| Phase 5 signing | ~½ day wiring | ~$10/mo |
| Phase 6 macOS | +1–2 days | $99/yr |

**Minimum viable "real app"** = Phases 0–3 (~1 week of part-time work, $0):
installs properly, knows its version, tells you when to update, download is one
click from a stable page. Phase 4 makes the update itself one click. Phase 5
signing makes it look professional.

---

## Decisions for Minh / Joel

1. **Public or private repo?** → recommend **public** (no secrets in the
   updater). Anything blocking that?
2. **Own repo, or a `phd-lab-tools` monorepo** with `ssn_plotter`,
   `nucleation_mindmap`, `literature_map`? If they'll all get this treatment,
   one repo with per-app tags (`data-acquisition-v0.4.0`) shares the CI.
3. **Pay ~$10/mo for code signing?** High value if Joel wants it to spread
   beyond people who'll tolerate "Run anyway".
4. **Updater: start home-rolled, migrate to tufup later — OK?**
5. **Product name** — keep `data_acquisition` internally, or name it now?
6. **Who's the maintainer** long-term (Minh through the PhD, then handover to
   Joel / the next postdoc)? Affects how much to invest in automation vs. docs.

---

_Companion: `DISTRIBUTION.md` (current manual process), `README.md` (design),
`build.py` (current build). This plan supersedes the "post-MVP roadmap" bullet
list at the bottom of `README.md`._
