"""Candle type, IST session helpers and session-anchored resampling (TradingView NSE style)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, List

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


@dataclass
class Candle:
    time: datetime          # bar OPEN time, tz-aware IST
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def ts(self) -> int:
        return int(self.time.timestamp())


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def session_open_dt(d: datetime) -> datetime:
    return datetime.combine(d.date(), SESSION_OPEN, tzinfo=IST)


def session_close_dt(d: datetime) -> datetime:
    return datetime.combine(d.date(), SESSION_CLOSE, tzinfo=IST)


def bucket_bounds(t: datetime, tf_min: int):
    """Session-anchored (09:15) bucket [start, end) containing bar time t. Last bucket is cut at 15:30."""
    t = to_ist(t)
    so = session_open_dt(t)
    mins = int((t - so).total_seconds() // 60)
    if mins < 0:
        mins = 0
    start = so + timedelta(minutes=(mins // tf_min) * tf_min)
    end = min(start + timedelta(minutes=tf_min), session_close_dt(t))
    return start, end


def resample(candles: Iterable[Candle], tf_min: int, base_min: int = 1) -> List[Candle]:
    """Aggregate base candles (e.g. 1m) into tf_min candles anchored at 09:15 IST.
    Candles outside the 09:15-15:30 session are dropped."""
    out: List[Candle] = []
    cur: Candle | None = None
    cur_start = None
    for c in candles:
        t = to_ist(c.time)
        if not (SESSION_OPEN <= t.time() < SESSION_CLOSE):
            continue
        start, _ = bucket_bounds(t, tf_min)
        if cur is None or start != cur_start:
            if cur is not None:
                out.append(cur)
            cur = Candle(start, c.open, c.high, c.low, c.close, c.volume)
            cur_start = start
        else:
            cur.high = max(cur.high, c.high)
            cur.low = min(cur.low, c.low)
            cur.close = c.close
            cur.volume += c.volume
    if cur is not None:
        out.append(cur)
    return out


def closed_only(candles: List[Candle], tf_min: int, now: datetime) -> List[Candle]:
    """Drop the still-forming last bucket."""
    res = []
    for c in candles:
        _, end = bucket_bounds(c.time, tf_min)
        if end <= now:
            res.append(c)
    return res
