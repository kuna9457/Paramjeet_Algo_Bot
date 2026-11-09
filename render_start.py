"""Render entry point (free plan: no disk, so everything important lives in MongoDB).

1. restores the settings saved from the dashboard (stored in MongoDB) onto the fresh disk
2. clears stale STOP / KILL files
3. AUTO_START_BOT=paper -> starts the paper-trading bot in the background (it waits for the daily broker login)
4. runs the Streamlit dashboard on Render's $PORT
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def pull_settings() -> None:
    uri = os.getenv("MONGO_URI", "").strip()
    if not uri:
        return
    try:
        from pymongo import MongoClient

        from bot.config import DEFAULT_SETTINGS, load_settings
        c = MongoClient(uri, serverSelectionTimeoutMS=8000)
        doc = c[os.getenv("MONGO_DB", "nexus_smc_bot")]["config"].find_one({"id": "settings"})
        if not doc or not doc.get("text"):
            print("[render_start] no saved settings in MongoDB - using the defaults from the repo", flush=True)
            return
        tmp = DEFAULT_SETTINGS.with_name("settings.pulled.yaml")
        tmp.write_text(doc["text"], encoding="utf-8")
        load_settings(tmp)                       # validate before using
        tmp.replace(DEFAULT_SETTINGS)
        print("[render_start] settings restored from MongoDB", flush=True)
    except Exception as e:  # noqa
        print(f"[render_start] could not restore settings: {e}", flush=True)


def main() -> None:
    from bot.ui import process

    pull_settings()
    process.STOP_FILE.unlink(missing_ok=True)
    process.KILL_FILE.unlink(missing_ok=True)

    mode = (os.getenv("AUTO_START_BOT") or "off").strip().lower()
    if mode == "paper":
        try:
            rec = process.start("paper")
            print(f"[render_start] paper bot started, pid {rec['pid']}", flush=True)
        except Exception as e:  # noqa
            print(f"[render_start] could not auto-start the bot: {e}", flush=True)
    elif mode == "live":
        print("[render_start] AUTO_START_BOT=live is refused: Render has no fixed outgoing IP, so brokers "
              "reject live orders. Use paper mode here.", flush=True)

    cmd = [sys.executable, "-m", "streamlit", "run", "dashboard.py",
           "--server.address", "0.0.0.0", "--server.port", os.getenv("PORT", "8501"),
           "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    if os.name == "nt":                       # local test on Windows
        sys.exit(subprocess.call(cmd))
    os.execv(sys.executable, cmd)             # Linux (Render): Streamlit becomes the main process


if __name__ == "__main__":
    main()
