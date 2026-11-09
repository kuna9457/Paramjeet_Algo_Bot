"""Start / stop the trading bot as a background process from the dashboard."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Optional

from ..config import ROOT
from ..data.candle import now_ist

PID_FILE = ROOT / "logs" / "bot_process.json"
CONSOLE_LOG = ROOT / "logs" / "bot_console.log"
STOP_FILE = ROOT / "STOP"
KILL_FILE = ROOT / "KILL"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                             text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return f'"{pid}"' in out
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
    return rec if _alive(int(rec.get("pid", 0))) else None


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
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
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


def force_stop() -> bool:
    rec = info()
    if not rec:
        return False
    pid = int(rec["pid"])
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        import signal
        os.kill(pid, signal.SIGTERM)
    return True


def tail(path, lines: int = 300) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 400_000))
        data = fh.read().decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-lines:])
