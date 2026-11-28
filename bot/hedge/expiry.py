"""Expiry selection from the broker's list of live NIFTY option expiries."""
from __future__ import annotations

from datetime import date
from typing import List, Optional


def monthly_expiries(expiries: List[date]) -> List[date]:
    """The last expiry of each calendar month = monthly contract."""
    last = {}
    for e in sorted(expiries):
        last[(e.year, e.month)] = e
    return sorted(last.values())


EXPIRY_MODES = ("min_days", "nearest_weekly", "next_weekly", "current_monthly", "next_monthly", "fixed_date")


def select_expiry(expiries: List[date], mode: str, today: date, fixed: str = "",
                  use_next_on_expiry_day: bool = True, min_days: int = 7, max_days: int = 0) -> Optional[date]:
    live = sorted(e for e in expiries if e >= today)
    if mode == "min_days":
        # first expiry (weekly or monthly) with at least min_days left, and at most max_days (0 = no limit)
        ok = [e for e in live if (e - today).days >= int(min_days) and (not max_days or (e - today).days <= int(max_days))]
        return ok[0] if ok else None
    if use_next_on_expiry_day:
        live = [e for e in live if e > today] or live
    if not live:
        return None
    if mode == "fixed_date":
        if not fixed:
            raise ValueError("hedge.expiry_date must be set when expiry_mode = fixed_date")
        d = date.fromisoformat(fixed)
        return d if d in expiries else None
    if mode == "nearest_weekly":
        return live[0]
    if mode == "next_weekly":
        return live[1] if len(live) > 1 else live[0]
    months = [e for e in monthly_expiries(expiries) if e in live]
    if not months:
        return None
    if mode == "current_monthly":
        return months[0]
    if mode == "next_monthly":
        return months[1] if len(months) > 1 else months[0]
    raise ValueError(f"Unknown expiry_mode {mode}")


def choose_expiry(expiries: List[date], mode: str, today: date, fixed: str = "",
                  use_next_on_expiry_day: bool = True, min_days: int = 7, max_days: int = 0):
    """select_expiry + safe fallback. Returns (expiry, note). If the exact date the client picked is no longer
    listed (expired / not yet launched by the exchange) the nearest upcoming expiry is used and `note` says so."""
    exp = select_expiry(expiries, mode, today, fixed, use_next_on_expiry_day, min_days, max_days)
    if exp is None and mode == "min_days":
        return None, (f"no expiry with {min_days}" + (f"-{max_days}" if max_days else "+") + " days left")
    if exp is None and mode == "fixed_date":
        exp = select_expiry(expiries, "nearest_weekly", today, "", use_next_on_expiry_day)
        if exp:
            return exp, f"chosen expiry {fixed} is not available - using nearest expiry {exp}"
    return exp, ""


def expiry_for(expiries: List[date], H, today: date):
    """choose_expiry with every option read from the hedge settings section."""
    return choose_expiry(expiries, H.get("expiry_mode", "nearest_weekly"), today, H.get("expiry_date", ""),
                         bool(H.get("use_next_expiry_on_expiry_day", True)), int(H.get("min_days_to_expiry", 7) or 0),
                         int(H.get("max_days_to_expiry", 0) or 0))


def expiry_options(expiries: List[date], today: date) -> List[dict]:
    """Exact upcoming expiries for a dropdown: [{date, label}] with weekly/monthly tag, nearest first."""
    live = sorted(e for e in set(expiries) if e >= today)
    months = set(monthly_expiries(live))
    return [{"date": e.isoformat(), "label": f"{e.isoformat()} ({e.strftime('%a')}) - {'monthly' if e in months else 'weekly'}"}
            for e in live]


def save_expiry_list(storage, symbol: str, expiries: List[date]) -> None:
    """Remember the broker's real expiry list so the dashboard can offer it (works for local and Mongo storage)."""
    from datetime import datetime
    storage.upsert("config", {"id": "expiries"}, {"id": "expiries", "symbol": symbol,
                                                  "expiries": sorted(e.isoformat() for e in set(expiries)),
                                                  "saved_at": datetime.now().isoformat(timespec="seconds")})


def load_expiry_list(storage) -> List[date]:
    doc = storage.find_one("config", {"id": "expiries"}) or {}
    out = []
    for x in doc.get("expiries", []):
        try:
            out.append(date.fromisoformat(str(x)))
        except ValueError:
            pass
    return out
