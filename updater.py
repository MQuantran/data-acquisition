"""Update check for data_acquisition.

Asks the GitHub Releases API whether a newer version has been published, so the
app can show a "Check for updates" result. Standard library only -- no extra
dependency, works from the frozen .exe.

Everything here is best-effort. No internet, no GitHub, no releases yet, a rate
limit -- each maps to a clear ``status`` string, never an exception into the
GUI.

    from updater import check_for_update
    res = check_for_update("0.4.0")
    if res.status == "update":
        webbrowser.open(res.page_url)      # or res.download_url

The release the app checks against lives at:

    https://github.com/<REPO>/releases

Point ``REPO`` below at wherever the releases are published (must match the repo
you push to). Override at runtime for testing with the env var
``DAQ_UPDATE_REPO=owner/name``.

Headless checks:
    python updater.py --selftest     offline: version parsing + comparison
    python updater.py --live         actually hits GitHub and prints the result
"""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

# --- where releases are published -------------------------------------------
REPO = os.environ.get("DAQ_UPDATE_REPO", "MQuantran/data-acquisition")
_API = "https://api.github.com/repos/{repo}/releases/latest"
_RELEASES_PAGE = "https://github.com/{repo}/releases/latest"
TIMEOUT = 6.0  # seconds for the whole request


@dataclass
class UpdateResult:
    """Outcome of one update check.

    status:
        "update"     -- a newer version is published (latest/notes/*_url set)
        "current"    -- you are on the newest release
        "no_release" -- the repo exists but has no published release yet
        "offline"    -- could not reach GitHub (no connection, timeout, DNS)
        "error"      -- reached GitHub but got something unexpected
    """
    status: str
    current: str
    latest: str = ""
    notes: str = ""
    page_url: str = ""
    download_url: str = ""
    message: str = ""


def parse_version(tag: str) -> tuple[int, int, int]:
    """'v0.4.1' or '0.4.1-beta+7' -> (0, 4, 1). Missing/odd parts -> 0."""
    s = (tag or "").strip().lstrip("vV").strip()
    for sep in ("-", "+", " "):
        s = s.split(sep, 1)[0]
    parts: list[int] = []
    for chunk in s.split(".")[:3]:
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def is_newer(remote: str, local: str) -> bool:
    """True if release tag ``remote`` is a higher version than ``local``."""
    return parse_version(remote) > parse_version(local)


def _pick_asset(assets: list[dict]) -> str:
    """Best direct-download URL: a win64 .zip, else any .zip, else any .exe."""
    def url(a: dict) -> str:
        return a.get("browser_download_url", "")

    zips = [a for a in assets if a.get("name", "").lower().endswith(".zip")]
    for a in zips:
        if "win" in a.get("name", "").lower():
            return url(a)
    if zips:
        return url(zips[0])
    exes = [a for a in assets if a.get("name", "").lower().endswith(".exe")]
    return url(exes[0]) if exes else ""


def check_for_update(current_version: str, repo: str | None = None,
                     timeout: float = TIMEOUT) -> UpdateResult:
    """Query GitHub for the latest release and compare it to this build."""
    repo = repo or REPO
    page = _RELEASES_PAGE.format(repo=repo)
    req = urllib.request.Request(
        _API.format(repo=repo),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"data_acquisition/{current_version}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return UpdateResult("no_release", current_version, page_url=page,
                                message="No releases have been published yet.")
        if e.code in (403, 429):
            return UpdateResult("error", current_version, page_url=page,
                                message="GitHub rate limit hit -- try again "
                                        "in a little while.")
        return UpdateResult("error", current_version, page_url=page,
                            message=f"GitHub returned HTTP {e.code}.")
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        return UpdateResult("offline", current_version, page_url=page,
                            message="Couldn't reach GitHub -- no internet "
                                    "connection?")
    except (ValueError, json.JSONDecodeError):
        return UpdateResult("error", current_version, page_url=page,
                            message="Unexpected response from GitHub.")

    tag = data.get("tag_name") or data.get("name") or ""
    latest = tag.lstrip("vV") or current_version
    notes = (data.get("body") or "").strip()
    html = data.get("html_url") or page
    download = _pick_asset(data.get("assets") or [])

    if tag and is_newer(tag, current_version):
        return UpdateResult("update", current_version, latest=latest,
                            notes=notes, page_url=html, download_url=download,
                            message=f"Version {latest} is available.")
    return UpdateResult("current", current_version, latest=latest,
                        page_url=html, message="You have the latest version.")


# --------------------------------------------------------------------------- #
def _selftest() -> int:
    cases = [
        (("1.0.0", "0.9.9"), True),
        (("v0.4.0", "0.4.0"), False),
        (("0.4.1", "v0.4.0"), True),
        (("0.10.0", "0.9.0"), True),        # numeric, not lexical
        (("0.4.0-beta", "0.3.9"), True),
        (("0.4.0", "0.4.0-beta"), False),   # 0.4.0-beta parses to (0,4,0)
        (("garbage", "0.1.0"), False),
    ]
    ok = True
    for (a, b), want in cases:
        got = is_newer(a, b)
        flag = "ok" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  is_newer({a!r}, {b!r}) = {got}  [{flag}]")
    assert parse_version("v2.11.3") == (2, 11, 3)
    assert parse_version("") == (0, 0, 0)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _live() -> int:
    res = check_for_update("0.0.0")  # force "update" if any release exists
    print(f"repo         {REPO}")
    print(f"status       {res.status}")
    print(f"latest       {res.latest}")
    print(f"page_url     {res.page_url}")
    print(f"download_url {res.download_url}")
    print(f"message      {res.message}")
    if res.notes:
        print("notes:\n" + res.notes[:400])
    return 0


if __name__ == "__main__":
    if "--live" in sys.argv:
        sys.exit(_live())
    sys.exit(_selftest())
