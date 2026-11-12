"""Start / stop the trading bot as a background process from the dashboard."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Optional

from ..config import ROOT
from ..data.candle import now_ist

PID_FILE = ROOT / "logs" / "bot_process.json"
CONSOLE_LOG = ROOT / "logs" / "bot_console.log"
STOP_FILE = ROOT / "STOP"
KILL_FILE = ROOT / "KILL"
STOP_GRACE_SEC = 45   # after this a bot that ignores the stop request is force-killed


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                             text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return f'"{pid}"' in out and "python" in out.lower()   # guards against a reused PID
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def info() -> Optional[dict]:
    """Process record if the bot started from the dashboard is still alive."""
    if not PID_FILE.exists():
        return None
    try:
        rec = json.loads(PID_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa
        return None
    if not _alive(int(rec.get("pid", 0))):
        PID_FILE.unlink(missing_ok=True)
        return None
    # A stop was requested but the process is still alive after the grace period -> it is hung; kill it.
    if STOP_FILE.exists() and time.time() - STOP_FILE.stat().st_mtime > STOP_GRACE_SEC:
        _kill(int(rec["pid"]))
        PID_FILE.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)
        return None
    return rec


def start(mode: str) -> dict:
    if mode not in ("paper", "live"):
        raise ValueError("mode must be paper or live")
    if info():
        raise RuntimeError("bot is already running")
    STOP_FILE.unlink(missing_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    log = open(CONSOLE_LOG, "a", encoding="utf-8")
    log.write(f"\n===== {now_ist():%Y-%m-%d %H:%M:%S} start --mode {mode} =====\n")
    log.flush()
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    env = dict(os.environ, NEXUS_FROM_DASHBOARD="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    p = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "run", "--mode", mode], cwd=str(ROOT),
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, **kw)
    rec = {"pid": p.pid, "mode": mode, "started_at": now_ist().isoformat()}
    PID_FILE.write_text(json.dumps(rec), encoding="utf-8")
    return rec


def request_stop() -> None:
    """Graceful: the bot finishes its current step and exits; open combos stay open."""
    STOP_FILE.write_text(now_ist().isoformat(), encoding="utf-8")


def kill_switch(on: bool) -> None:
    """Kill switch: the bot squares off every open combo and stops. Stays on until cleared."""
    if on:
        KILL_FILE.write_text(now_ist().isoformat(), encoding="utf-8")
    else:
        KILL_FILE.unlink(missing_ok=True)


def _kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        import signal
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def force_stop(storage=None) -> bool:
    """Kill the process, clear the stale files and mark the status STOPPED so Start works immediately."""
    rec = info()
    if rec:
        _kill(int(rec["pid"]))
    PID_FILE.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    if storage is not None:
        try:
            storage.upsert("status", {"id": "bot"}, {"state": "STOPPED", "msg": "force stopped from dashboard",
                                                     "updated_at": now_ist().isoformat()})
        except Exception:  # noqa
            pass
    return bool(rec)


def tail(path, lines: int = 300) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 400_000))
        data = fh.read().decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-lines:])
