"""Daily broker access tokens: .env value wins, otherwise today's token saved via `main.py login`."""
from __future__ import annotations

from datetime import timedelta

from ..config import env
from ..data.candle import now_ist
from .storage import Storage

ENV_KEYS = {"upstox": "UPSTOX_ACCESS_TOKEN", "zerodha": "KITE_ACCESS_TOKEN"}


def token_day() -> str:
    # tokens expire early morning; a token created after 06:00 is valid for "today"
    n = now_ist()
    if n.hour < 6:
        n = n - timedelta(days=1)
    return n.date().isoformat()


def save_token(storage: Storage, broker: str, token: str) -> None:
    storage.upsert("tokens", {"broker": broker}, {"broker": broker, "token": token, "day": token_day(),
                                                    "saved_at": now_ist().isoformat()})


def get_token(storage: Storage, broker: str) -> str:
    t = env(ENV_KEYS[broker])
    if t:
        return t
    doc = storage.find_one("tokens", {"broker": broker})
    if doc and doc.get("day") == token_day():
        return doc["token"]
    return ""
