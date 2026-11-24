"""Option hedge scanner (runs only AFTER a strategy signal - it never affects signal generation).

BUY  signal -> SELL PUT  (premium inside sell_min_premium..sell_max_premium, sell_strike_rule)
             + BUY  PUT  at a LOWER strike  (hedge, buy_strike_rule)
SELL signal -> SELL CALL (same sell rules)
             + BUY  CALL at a HIGHER strike (hedge, same rule)

The hedge is chosen from the premium gap:  SellPremium - BuyPremium  must be inside hedge_diff_min..hedge_diff_max
(e.g. sell 52, range 10-15 -> hedge premium 37..42). A limit of 0 means "no limit on that side".
When several pairs qualify, the bot sells the option with the highest premium that still fits the sell band
(closest to sell_max_premium, e.g. 55), then hedges with the nearest qualifying strike beyond it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..brokers.base import OptionQuote


@dataclass
class HedgePair:
    direction: int
    buy: OptionQuote          # hedge leg (bought)
    sell: OptionQuote         # premium leg (sold)
    ref_price: float
    atm: float
    candidates: int = 0

    @property
    def net(self) -> float:
        """Net premium per unit at scan time (sell - buy): + = received, - = paid."""
        return round(self.sell.ltp - self.buy.ltp, 2)

    diff = net                # older name, still stored on combos as scan_diff

    def describe(self) -> str:
        return (f"SELL {int(self.sell.strike)} {self.sell.opt_type} @ {self.sell.ltp:.2f}  +  "
                f"BUY {int(self.buy.strike)} {self.buy.opt_type} @ {self.buy.ltp:.2f} (hedge)")


STRIKE_RULES = ("any", "ends_with_00", "ends_with_50")
SELL_BAND = ("sell_min_premium", "sell_max_premium")
DIFF_RANGE = ("hedge_diff_min", "hedge_diff_max")


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


def band(cfg, lo_key: str, hi_key: str) -> Tuple[float, float]:
    return float(cfg.get(lo_key, 0) or 0), float(cfg.get(hi_key, 0) or 0)


def _in_band(price: float, lo: float, hi: float) -> bool:
    return (not lo or price >= lo) and (not hi or price <= hi)


def _band_text(lo: float, hi: float) -> str:
    if lo and hi:
        return f"{lo:g}-{hi:g}"
    if lo:
        return f">= {lo:g}"
    return f"<= {hi:g}" if hi else "any"


def range_text(cfg) -> str:
    """Short description of the leg rules, used in alerts and the signal log."""
    return (f"sell premium {_band_text(*band(cfg, *SELL_BAND))} ({cfg.get('sell_strike_rule', 'ends_with_50')}), "
            f"sell - hedge premium {_band_text(*band(cfg, *DIFF_RANGE))} ({cfg.get('buy_strike_rule', 'ends_with_50')})")


def find_pair(direction: int, chain: List[OptionQuote], ref_price: float, cfg) -> Tuple[Optional[HedgePair], List[HedgePair]]:
    step = int(cfg.get("strike_step", 50))
    width = int(cfg.get("strike_scan_width", 10))
    min_prem = float(cfg.get("min_leg_premium", 0) or 0)
    buy_rule = cfg.get("buy_strike_rule", "ends_with_50")
    sell_rule = cfg.get("sell_strike_rule", "ends_with_50")
    sell_band, diff_range = band(cfg, *SELL_BAND), band(cfg, *DIFF_RANGE)
    atm, window = strike_window(ref_price, step, width)
    wset = set(window)

    opt = "PE" if direction == 1 else "CE"
    quotes = [q for q in chain if q.opt_type == opt and q.strike in wset and q.ltp >= min_prem]
    sells = [q for q in quotes if _strike_ok(q.strike, sell_rule) and _in_band(q.ltp, *sell_band)]
    buys = [q for q in quotes if _strike_ok(q.strike, buy_rule)]

    matches: List[HedgePair] = []
    for s in sells:
        for b in buys:
            beyond = b.strike < s.strike if direction == 1 else b.strike > s.strike   # hedge is further out of the money
            if beyond and _in_band(round(s.ltp - b.ltp, 2), *diff_range):
                matches.append(HedgePair(direction, b, s, ref_price, atm))
    matches.sort(key=lambda p: (-p.sell.ltp, abs(p.buy.strike - p.sell.strike)))
    if not matches:
        return None, []
    best = matches[0]
    best.candidates = len(matches)
    return best, matches
