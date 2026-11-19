"""Premium-difference strike scanner (runs only AFTER a strategy signal - it never affects signal generation).

BUY  signal -> BUY CE (any strike)  + SELL PE (strike ending in 50)
SELL signal -> BUY PE (any strike)  + SELL CE (strike ending in 50)

hedge.pair_mode = "debit"  (default, original behaviour)
    diff = BuyLegLTP - SellLegLTP ; must be positive and range_min <= diff <= range_max   (you pay a net debit)
hedge.pair_mode = "credit"
    diff = SellLegLTP - BuyLegLTP ; must be credit_min <= diff <= credit_max              (you receive a net credit)
    sell leg premium <= sell_max_premium (0 = no cap), buy leg premium inside buy_min_premium..buy_max_premium
    (0 = no limit on that side).

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
    diff: float               # the amount that was range-checked: net debit (debit mode) or net credit (credit mode)
    ref_price: float
    atm: float
    candidates: int = 0
    mode: str = "debit"

    @property
    def net(self) -> float:
        """Signed net premium per unit at scan time: + = credit received, - = debit paid."""
        return self.diff if self.mode == "credit" else -self.diff

    def describe(self) -> str:
        kind = "net CREDIT" if self.mode == "credit" else "net DEBIT"
        return (f"BUY {int(self.buy.strike)} {self.buy.opt_type} @ {self.buy.ltp:.2f}  +  "
                f"SELL {int(self.sell.strike)} {self.sell.opt_type} @ {self.sell.ltp:.2f}  "
                f"({kind} {self.diff:.2f})")


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


def pair_mode(cfg) -> str:
    return str(cfg.get("pair_mode", "debit") or "debit").strip().lower()


def diff_range(cfg) -> Tuple[float, float]:
    """(min, max) of the net premium range that applies to the configured pair mode."""
    if pair_mode(cfg) == "credit":
        return float(cfg.get("credit_min", 10)), float(cfg.get("credit_max", 15))
    return float(cfg.get("range_min")), float(cfg.get("range_max"))


def range_text(cfg) -> str:
    lo, hi = diff_range(cfg)
    if pair_mode(cfg) != "credit":
        return f"net debit {lo:g}-{hi:g}"
    extra = []
    if float(cfg.get("sell_max_premium", 0) or 0):
        extra.append(f"sell premium <= {float(cfg.get('sell_max_premium')):g}")
    bmin, bmax = float(cfg.get("buy_min_premium", 0) or 0), float(cfg.get("buy_max_premium", 0) or 0)
    if bmin or bmax:
        extra.append(f"buy premium {bmin:g}-{bmax:g}" if bmin and bmax else
                     (f"buy premium >= {bmin:g}" if bmin else f"buy premium <= {bmax:g}"))
    return f"net credit {lo:g}-{hi:g}" + (" (" + ", ".join(extra) + ")" if extra else "")


def find_pair(direction: int, chain: List[OptionQuote], ref_price: float, cfg) -> Tuple[Optional[HedgePair], List[HedgePair]]:
    step = int(cfg.get("strike_step", 50))
    width = int(cfg.get("strike_scan_width", 10))
    mode = pair_mode(cfg)
    rmin, rmax = diff_range(cfg)
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

    if mode == "credit":
        sell_cap = float(cfg.get("sell_max_premium", 0) or 0)
        bmin, bmax = float(cfg.get("buy_min_premium", 0) or 0), float(cfg.get("buy_max_premium", 0) or 0)
        buys = [q for q in buys if (not bmin or q.ltp >= bmin) and (not bmax or q.ltp <= bmax)]
        sells = [q for q in sells if not sell_cap or q.ltp <= sell_cap]

    mid = (rmin + rmax) / 2.0
    matches: List[HedgePair] = []
    for b in buys:
        for s in sells:
            diff = round((s.ltp - b.ltp) if mode == "credit" else (b.ltp - s.ltp), 2)
            if diff > 0 and rmin <= diff <= rmax:
                matches.append(HedgePair(direction, b, s, diff, ref_price, atm, mode=mode))
    matches.sort(key=lambda p: (abs(p.sell.strike - ref_price), abs(p.buy.strike - ref_price), abs(p.diff - mid)))
    if not matches:
        return None, []
    best = matches[0]
    best.candidates = len(matches)
    return best, matches
