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


def select_expiry(expiries: List[date], mode: str, today: date, fixed: str = "",
                  use_next_on_expiry_day: bool = True) -> Optional[date]:
    live = sorted(e for e in expiries if e >= today)
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
