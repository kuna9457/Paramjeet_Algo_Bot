"""Synthetic market data + fake broker for offline tests."""
from __future__ import annotations

import math
import random
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List

from bot.brokers.base import Broker, Instrument, OptionQuote, OrderResult
from bot.data.candle import IST, Candle


def synthetic_1m(days: int = 60, seed: int = 7, start_price: float = 23500.0) -> List[Candle]:
    """Trending + mean-reverting random walk, 375 one-minute bars per weekday session."""
    rnd = random.Random(seed)
    px = start_price
    out: List[Candle] = []
    d = date(2025, 1, 6)
    made = 0
    drift = 0.0
    while made < days:
        if d.weekday() < 5:
            t = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
            px += rnd.gauss(0, 40)  # overnight gap
            for i in range(375):
                if i % 60 == 0:
                    drift = rnd.choice([-1, 1]) * rnd.uniform(0.0, 1.2)
                o = px
                c = o + drift + rnd.gauss(0, 6)
                h = max(o, c) + abs(rnd.gauss(0, 3))
                l = min(o, c) - abs(rnd.gauss(0, 3))
                out.append(Candle(t + timedelta(minutes=i), round(o, 2), round(h, 2), round(l, 2), round(c, 2), 1000))
                px = c
            made += 1
        d += timedelta(days=1)
    return out


class FakeBroker(Broker):
    """In-memory broker: Black-Scholes-ish option prices around a moving spot."""
    name = "fake"

    def __init__(self, spot: float = 23480.0, expiries: List[date] | None = None):
        self.spot = spot
        self.expiries = expiries or [date(2026, 11, 10), date(2026, 11, 17), date(2026, 11, 24), date(2026, 12, 29)]
        self.orders: Dict[str, OrderResult] = {}
        self.placed: List[tuple] = []
        self.prices: Dict[str, float] = {}
        self.history: List[Candle] = []

    def connect(self):
        pass

    def load_instruments(self):
        pass

    def signal_instrument(self, s):
        return Instrument("IDX|NIFTY", "NIFTY 50", None, None, "IDX")

    def option_expiries(self):
        return list(self.expiries)

    def option_instrument(self, expiry, strike, t):
        sym = f"NIFTY{expiry:%y%m%d}{int(strike)}{t}"
        return Instrument(f"FO|{sym}", sym, expiry, float(strike), t, lot_size=75)

    def _opt_price(self, strike: float, t: str) -> float:
        intrinsic = max(self.spot - strike, 0) if t == "CE" else max(strike - self.spot, 0)
        tv = 120 * math.exp(-abs(self.spot - strike) / 250)
        return round(intrinsic + tv, 2)

    def ltp(self, insts):
        out = {}
        for i in insts:
            if i.key in self.prices:
                out[i.key] = self.prices[i.key]
            elif i.opt_type == "IDX" or i.key.startswith("IDX"):
                out[i.key] = self.spot
            else:
                sym = i.symbol
                strike = float(sym[-7:-2])
                out[i.key] = self._opt_price(strike, sym[-2:])
        return out

    def candles_1m(self, inst, start, end):
        return [c for c in self.history if start <= c.time <= end]

    def candles_1m_today(self, inst):
        return []

    def place_order(self, inst, side, qty, order_type, price, product, tag=""):
        oid = uuid.uuid4().hex[:8]
        px = self.ltp([inst])[inst.key]
        self.orders[oid] = OrderResult("COMPLETE", qty, px)
        self.placed.append((side, inst.symbol, qty, px))
        return oid

    def order_status(self, oid):
        return self.orders[oid]

    def cancel_order(self, oid):
        pass
