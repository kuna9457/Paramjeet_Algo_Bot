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
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
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


def _is_cert_bundle_error(e: BaseException) -> bool:
    return "TLS CA certificate bundle" in str(e) or "certificate verify failed" in str(e)


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub private-asset downloads redirect to another host, which must not receive the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


def _urlopen(url: str, headers: dict, timeout: float):
    """urllib + Windows' own certificate store. Used when the bundled certifi/cacert.pem is missing or broken
    (e.g. the install folder was moved, or an antivirus removed the file), so updating never depends on it."""
    opener = urllib.request.build_opener(_DropAuthOnRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)


def fetch_latest(repo: str, token: str = "") -> Optional[dict]:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        import requests
        r = requests.get(url, headers=_headers(token), timeout=8)
        return r.json() if r.status_code == 200 else None
    except OSError as e:
        if not _is_cert_bundle_error(e):
            raise
    try:
        with _urlopen(url, _headers(token), 8) as r:
            return json.loads(r.read().decode("utf-8")) if r.status == 200 else None
    except urllib.error.HTTPError:        # same as requests path: any non-200 -> "no update info"
        return None


def _asset(release: dict, pattern: str) -> Optional[dict]:
    for a in release.get("assets", []):
        if re.match(pattern, a.get("name", "")):
            return a
    return None


def _download(asset: dict, token: str, dest: Path) -> None:
    if token:   # private repo: API asset URL + octet-stream
        url, hdr = asset["url"], _headers(token, "application/octet-stream")
    else:
        url, hdr = asset["browser_download_url"], {"User-Agent": "nexus-smc-bot-updater"}
    try:
        import requests
        with requests.get(url, headers=hdr, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        return
    except OSError as e:
        if not _is_cert_bundle_error(e):
            raise
    with _urlopen(url, hdr, 60) as r, open(dest, "wb") as f:      # fallback: Windows certificate store
        shutil.copyfileobj(r, f, 1 << 20)


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
