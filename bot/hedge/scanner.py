"""Premium-difference strike scanner.

BUY  signal -> BUY CE (any strike)  + SELL PE (strike ending in 50)
SELL signal -> BUY PE (any strike)  + SELL CE (strike ending in 50)
diff = BuyLegLTP - SellLegLTP ; must be positive and range_min <= diff <= range_max.
If several pairs qualify: sell strike closest to the reference price, then buy strike closest,
then diff closest to the middle of the range.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..brokers.base import OptionQuote


@dataclass
class HedgePair:
    direction: int
    buy: OptionQuote
    sell: OptionQuote
    diff: float
    ref_price: float
    atm: float
    candidates: int = 0

    def describe(self) -> str:
        return (f"BUY {int(self.buy.strike)} {self.buy.opt_type} @ {self.buy.ltp:.2f}  +  "
                f"SELL {int(self.sell.strike)} {self.sell.opt_type} @ {self.sell.ltp:.2f}  "
                f"(diff {self.diff:.2f})")


def _strike_ok(strike: float, rule: str) -> bool:
    k = int(round(strike))
    if rule == "any":
        return True
    if rule == "ends_with_50":
        return k % 100 == 50
    if rule == "ends_with_00":
        return k % 100 == 0
    raise ValueError(f"unknown strike rule {rule}")


def strike_window(ref_price: float, step: int, width: int) -> Tuple[float, List[float]]:
    atm = round(ref_price / step) * step
    return float(atm), [float(atm + i * step) for i in range(-width, width + 1)]


def find_pair(direction: int, chain: List[OptionQuote], ref_price: float, cfg) -> Tuple[Optional[HedgePair], List[HedgePair]]:
    step = int(cfg.get("strike_step", 50))
    width = int(cfg.get("strike_scan_width", 10))
    rmin, rmax = float(cfg.get("range_min")), float(cfg.get("range_max"))
    min_prem = float(cfg.get("min_leg_premium", 0))
    buy_rule = cfg.get("buy_strike_rule", "any")
    sell_rule = cfg.get("sell_strike_rule", "ends_with_50")
    atm, window = strike_window(ref_price, step, width)
    wset = set(window)

    buy_type, sell_type = ("CE", "PE") if direction == 1 else ("PE", "CE")
    buys = [q for q in chain if q.opt_type == buy_type and q.strike in wset and q.ltp >= min_prem
            and _strike_ok(q.strike, buy_rule)]
    sells = [q for q in chain if q.opt_type == sell_type and q.strike in wset and q.ltp >= min_prem
             and _strike_ok(q.strike, sell_rule)]

    mid = (rmin + rmax) / 2.0
    matches: List[HedgePair] = []
    for b in buys:
        for s in sells:
            diff = round(b.ltp - s.ltp, 2)
            if diff > 0 and rmin <= diff <= rmax:
                matches.append(HedgePair(direction, b, s, diff, ref_price, atm))
    matches.sort(key=lambda p: (abs(p.sell.strike - ref_price), abs(p.buy.strike - ref_price), abs(p.diff - mid)))
    if not matches:
        return None, []
    best = matches[0]
    best.candidates = len(matches)
    return best, matches
