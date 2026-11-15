"""Docker entry point. Same idea as render_start.py (which is left untouched), but allows live mode.

AUTO_START_BOT = off (default) | paper | live
  off   -> only the dashboard starts; press Start in the dashboard
  paper -> paper bot starts automatically whenever the container (re)starts
  live  -> LIVE bot starts automatically whenever the container (re)starts  (real orders!)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from render_start import pull_settings  # noqa: E402  (restores dashboard-saved settings from MongoDB if MONGO_URI is set)


def main() -> None:
    from bot.ui import process

    pull_settings()
    process.STOP_FILE.unlink(missing_ok=True)
    process.KILL_FILE.unlink(missing_ok=True)

    mode = (os.getenv("AUTO_START_BOT") or "off").strip().lower()
    if mode in ("paper", "live"):
        try:
            rec = process.start(mode)
            print(f"[docker_start] {mode} bot started, pid {rec['pid']}", flush=True)
        except Exception as e:  # noqa
            print(f"[docker_start] could not auto-start the bot: {e}", flush=True)

    cmd = [sys.executable, "-m", "streamlit", "run", "dashboard.py",
           "--server.address", "0.0.0.0", "--server.port", "8501",
           "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    main()
