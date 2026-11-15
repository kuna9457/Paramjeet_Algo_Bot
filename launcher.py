"""Nexus SMC Bot - desktop launcher (used by the Windows installer; does not change any trading logic).

  pythonw launcher.py          start the dashboard (if not already running), open the browser, check for updates
  pythonw launcher.py stop     stop the dashboard (a running trading bot is NOT touched)
  python  launcher.py version  print the installed version

The trading bot itself is started/stopped from the dashboard, exactly as before.
.env (broker keys) is edited in the dashboard: Login & connections -> "API keys (.env)".
AUTO_START_BOT=off|paper|live in .env behaves like the Docker version (default off).
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

PORT = 8501
URL = f"http://localhost:{PORT}"
LOGS = ROOT / "logs"
PID_FILE = LOGS / "dashboard.pid"
LOG_FILE = LOGS / "dashboard.log"
TITLE = "Nexus SMC Bot"
YES = 6


def msg(text: str, flags: int = 0x40) -> int:
    """Windows message box. flags: 0x40 info, 0x10 error, 0x24 yes/no question."""
    try:
        return ctypes.windll.user32.MessageBoxW(0, text, TITLE, flags)
    except Exception:  # noqa
        print(text)
        return YES


def log(text: str) -> None:
    try:
        LOGS.mkdir(exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [launcher] {text}\n")
    except Exception:  # noqa
        pass


def healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/_stcore/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa
        return False


def _python_exe() -> str:
    p = Path(sys.executable)
    cand = p.with_name("python.exe")          # pythonw.exe -> python.exe (child gets no console window anyway)
    return str(cand if cand.exists() else p)


def bot_running() -> bool:
    try:
        from bot.ui import process
        return process.info() is not None
    except Exception:  # noqa
        return False


def start_dashboard() -> bool:
    LOGS.mkdir(exist_ok=True)
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception as e:  # noqa
        log(f"dotenv: {e}")
    try:
        from render_start import pull_settings   # restores dashboard-saved settings from MongoDB (if MONGO_URI set)
        pull_settings()
    except Exception as e:  # noqa
        log(f"pull_settings: {e}")

    auto = (os.getenv("AUTO_START_BOT") or "off").strip().lower()
    try:
        from bot.ui import process
        if not process.info():                    # no bot running -> clear stale STOP/KILL files
            process.STOP_FILE.unlink(missing_ok=True)
            process.KILL_FILE.unlink(missing_ok=True)
            if auto in ("paper", "live"):
                rec = process.start(auto)
                log(f"{auto} bot auto-started, pid {rec['pid']}")
    except Exception as e:  # noqa
        log(f"auto-start: {e}")

    cmd = [_python_exe(), "-m", "streamlit", "run", "dashboard.py",
           "--server.address", "127.0.0.1", "--server.port", str(PORT),
           "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    out = open(LOG_FILE, "a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=env, creationflags=flags)
    PID_FILE.write_text(str(p.pid), encoding="utf-8")
    log(f"dashboard started, pid {p.pid}")
    for _ in range(90):
        if healthy():
            return True
        if p.poll() is not None:
            break
        time.sleep(1)
    return healthy()


def stop_dashboard() -> bool:
    """Stops ONLY the dashboard process (no /T, so a bot started from it keeps running)."""
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:  # noqa
        return False
    r = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    PID_FILE.unlink(missing_ok=True)
    log(f"dashboard stop requested, pid {pid}, rc {r.returncode}")
    return r.returncode == 0


def main() -> None:
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "open"
    if arg == "version":
        import updater
        print(updater.current_version())
        return
    if arg == "stop":
        quiet = "--silent" in sys.argv
        if bot_running() and not quiet:
            if msg("The trading BOT is still running.\n\nThis only closes the dashboard window/server; the bot keeps "
                   "trading. To stop the bot, open the dashboard and press Stop first.\n\nClose the dashboard now?",
                   0x24) != YES:
                return
        ok = stop_dashboard()
        if not quiet:
            msg("Dashboard stopped." if ok else "Dashboard was not running.")
        return

    if healthy():
        webbrowser.open(URL)
    else:
        if start_dashboard():
            webbrowser.open(URL)
        else:
            msg(f"The dashboard did not start.\n\nSee the log:\n{LOG_FILE}", 0x10)
            return
    try:                                          # update check never blocks or breaks the launcher
        import updater
        updater.check_for_update(before_install=stop_dashboard)
    except Exception as e:  # noqa
        log(f"update check: {e}")


if __name__ == "__main__":
    main()
