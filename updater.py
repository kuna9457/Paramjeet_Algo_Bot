"""Nexus SMC Bot - update check against GitHub Releases (used by launcher.py).

Looks at the latest release of `github_repo` (release_config.json). If it is newer than ./VERSION and has an asset
NexusBot_Setup_<ver>.exe plus NexusBot_Setup_<ver>.exe.sha256, it asks the user, downloads, verifies the SHA-256 and
runs the installer silently. The installer keeps .env, logs, data_cache and config/settings.yaml.
It never updates while the trading bot is running.

release_config.json:  {"github_repo": "owner/repo", "github_token": ""}
  - github_token is only needed if the repo is private (use a read-only token). A public releases-only repo needs none.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent
YES = 6


def _msg(text: str, flags: int = 0x40) -> int:
    try:
        return ctypes.windll.user32.MessageBoxW(0, text, "Nexus SMC Bot - update", flags)
    except Exception:  # noqa
        print(text)
        return YES


def current_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except Exception:  # noqa
        return "0.0.0"


def parse_version(v: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", str(v))[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def load_config() -> dict:
    try:
        return json.loads((ROOT / "release_config.json").read_text(encoding="utf-8"))
    except Exception:  # noqa
        return {}


def _headers(token: str, accept: str = "application/vnd.github+json") -> dict:
    h = {"Accept": accept, "User-Agent": "nexus-smc-bot-updater"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch_latest(repo: str, token: str = "") -> Optional[dict]:
    import requests
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", headers=_headers(token), timeout=8)
    if r.status_code != 200:
        return None
    return r.json()


def _asset(release: dict, pattern: str) -> Optional[dict]:
    for a in release.get("assets", []):
        if re.match(pattern, a.get("name", "")):
            return a
    return None


def _download(asset: dict, token: str, dest: Path) -> None:
    import requests
    if token:   # private repo: API asset URL + octet-stream
        url, hdr = asset["url"], _headers(token, "application/octet-stream")
    else:
        url, hdr = asset["browser_download_url"], {"User-Agent": "nexus-smc-bot-updater"}
    with requests.get(url, headers=hdr, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_for_update(before_install: Optional[Callable[[], object]] = None) -> None:
    cfg = load_config()
    repo = (cfg.get("github_repo") or "").strip()
    if not repo or "/" not in repo or repo.startswith("OWNER/"):
        return
    token = (cfg.get("github_token") or "").strip()
    rel = fetch_latest(repo, token)
    if not rel:
        return
    latest = str(rel.get("tag_name", ""))
    if parse_version(latest) <= parse_version(current_version()):
        return
    exe = _asset(rel, r"^NexusBot_Setup_.*\.exe$")
    sha = _asset(rel, r"^NexusBot_Setup_.*\.exe\.sha256$")
    if not exe or not sha:
        return                                    # incomplete release (installer or checksum not uploaded yet)

    try:
        from bot.ui import process
        if process.info():
            _msg(f"Version {latest} is available (you have {current_version()}).\n\nThe trading bot is running, so the "
                 "update is postponed. Stop the bot, close the dashboard and open Nexus SMC Bot again to update.")
            return
    except Exception:  # noqa
        pass

    notes = (rel.get("body") or "").strip()[:600]
    if _msg(f"A new version is available: {latest} (you have {current_version()}).\n\n{notes}\n\n"
            "Install it now? Your keys, settings and logs are kept.", 0x24) != YES:
        return

    tmp = Path(tempfile.mkdtemp(prefix="nexus_update_"))
    setup, sumfile = tmp / exe["name"], tmp / sha["name"]
    try:
        _download(exe, token, setup)
        _download(sha, token, sumfile)
        expected = sumfile.read_text(encoding="utf-8").split()[0].strip().lower()
        if _sha256(setup) != expected:
            _msg("The downloaded update failed its integrity check and was NOT installed.", 0x10)
            return
    except Exception as e:  # noqa
        _msg(f"Could not download the update:\n{e}", 0x10)
        return

    if before_install:
        before_install()
    # short delay so this launcher has exited and Windows releases the files before the installer replaces them
    cmd = f'cmd /c ping -n 4 127.0.0.1 >nul & "{setup}" /SILENT /NORESTART /SUPPRESSMSGBOXES'
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(cmd, shell=True, creationflags=flags, close_fds=True)
    os._exit(0)
