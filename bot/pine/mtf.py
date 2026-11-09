"""request.security(..., lookahead=barmerge.lookahead_off) emulation.

TradingView (historical / confirmed behaviour): the value computed on a higher-timeframe bar becomes
visible on the chart bar whose close completes that HTF bar. Until then, the chart sees the value
from the previous completed HTF bar. HTF bars are anchored to the NSE session open (09:15).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..data.candle import Candle, bucket_bounds
from .ta import EMA, pivot_high, pivot_low


class HTFContext:
    def __init__(self, tf_min: int, pivot_lengths: List[int] | None = None):
        self.tf = tf_min
        self.pivot_lengths = sorted(set(pivot_lengths or []))
        self.o: List[float] = []
        self.h: List[float] = []
        self.l: List[float] = []
        self.c: List[float] = []
        self.t: List[int] = []
        self.ema20 = EMA(20)
        self.ema20_hist: List[Optional[float]] = []
        self.last_ph: Dict[int, Optional[float]] = {L: None for L in self.pivot_lengths}
        self.last_pl: Dict[int, Optional[float]] = {L: None for L in self.pivot_lengths}
        self._cur: Optional[Candle] = None
        self._cur_start = None
        self._cur_end = None
        # snapshot visible to the chart
        self.snap: Dict[str, object] = self._empty_snap()

    @staticmethod
    def _empty_snap():
        return {"close_1": None, "ema20_1": None, "ph": {}, "pl": {}, "ohlct_1": (None,) * 5}

    def _finalize(self):
        c = self._cur
        self.o.append(c.open)
        self.h.append(c.high)
        self.l.append(c.low)
        self.c.append(c.close)
        self.t.append(c.ts)
        self.ema20_hist.append(self.ema20.update(c.close))
        for L in self.pivot_lengths:
            ph = pivot_high(self.h, L, L)
            pl = pivot_low(self.l, L, L)
            if ph is not None:
                self.last_ph[L] = ph
            if pl is not None:
                self.last_pl[L] = pl
        n = len(self.c)
        self.snap = {
            "close_1": self.c[-2] if n >= 2 else None,
            "ema20_1": self.ema20_hist[-2] if n >= 2 else None,
            "ph": dict(self.last_ph),
            "pl": dict(self.last_pl),
            "ohlct_1": (self.o[-2], self.h[-2], self.l[-2], self.c[-2], self.t[-2]) if n >= 2 else (None,) * 5,
        }
        self._cur = None
        self._cur_start = None

    def on_chart_bar(self, bar: Candle, chart_tf: int):
        """Feed one closed chart bar. Updates self.snap when an HTF bar completes."""
        start, end = bucket_bounds(bar.time, self.tf)
        if self._cur is not None and start != self._cur_start:
            self._finalize()  # previous HTF bar never reached its end (missing data)
        if self._cur is None:
            self._cur = Candle(start, bar.open, bar.high, bar.low, bar.close, bar.volume)
            self._cur_start, self._cur_end = start, end
        else:
            self._cur.high = max(self._cur.high, bar.high)
            self._cur.low = min(self._cur.low, bar.low)
            self._cur.close = bar.close
        _, bar_end = bucket_bounds(bar.time, chart_tf)
        if bar_end >= self._cur_end:
            self._finalize()
