"""Option hedge scanner (runs only AFTER a strategy signal - it never affects signal generation).

BUY signal -> SELL PUT + BUY a lower-strike PUT (hedge) · SELL signal -> SELL CALL + BUY a higher-strike CALL (hedge).

strike_mode = atm_offset (client sheet, default):
    sell the ATM option or one up to sell_otm_points out of the money (e.g. 0 / 50 pts),
    hedge with the option hedge_otm_points out of the money from ATM (e.g. 200 -> spread width 150-200).
    Optional sell-leg premium band atm_sell_min_premium .. atm_sell_max_premium (0 = no limit).
strike_mode = premium_band:
    sell the richest option inside sell_min_premium .. sell_max_premium (sell_strike_rule), hedge with the nearest
    strike beyond it where SellPremium - BuyPremium is inside hedge_diff_min .. hedge_diff_max (buy_strike_rule).
Both modes: each leg's bid-ask spread must be <= max_bid_ask_spread rupees (0 = off; quotes without depth pass).
When several pairs qualify, the one with the highest sell premium wins.
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
        def sp(q):
            return f" (bid-ask {q.spread:.2f})" if q.spread is not None else ""
        return (f"SELL {int(self.sell.strike)} {self.sell.opt_type} @ {self.sell.ltp:.2f}{sp(self.sell)}  +  "
                f"BUY {int(self.buy.strike)} {self.buy.opt_type} @ {self.buy.ltp:.2f}{sp(self.buy)} (hedge)")


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


STRIKE_MODES = ("atm_offset", "premium_band")
ATM_SELL_BAND = ("atm_sell_min_premium", "atm_sell_max_premium")


def _spread_ok(q: OptionQuote, max_spread: float) -> bool:
    sp = q.spread
    return not max_spread or sp is None or sp <= max_spread + 1e-9


def range_text(cfg) -> str:
    """Short description of the leg rules, used in alerts and the signal log."""
    sp = float(cfg.get("max_bid_ask_spread", 0) or 0)
    tail = f", bid-ask <= {sp:g}" if sp else ""
    if cfg.get("strike_mode", "premium_band") == "atm_offset":
        so, ho = int(cfg.get("sell_otm_points", 50) or 0), int(cfg.get("hedge_otm_points", 200) or 0)
        sell = "ATM" + (f" to {so} OTM" if so else "")
        band_ = band(cfg, *ATM_SELL_BAND)
        prem = f" (premium {_band_text(*band_)})" if any(band_) else ""
        sr, br = cfg.get("atm_sell_strike_rule", "any"), cfg.get("atm_buy_strike_rule", "any")
        sr = f" {sr}" if sr != "any" else ""
        br = f" {br}" if br != "any" else ""
        return f"sell {sell}{sr}{prem}, hedge ~{ho} OTM{br}{tail}"
    return (f"sell premium {_band_text(*band(cfg, *SELL_BAND))} ({cfg.get('sell_strike_rule', 'ends_with_50')}), "
            f"sell - hedge premium {_band_text(*band(cfg, *DIFF_RANGE))} ({cfg.get('buy_strike_rule', 'ends_with_50')})"
            + tail)


def _atm_pairs(direction, quotes, ref_price, atm, step, cfg) -> List[HedgePair]:
    otm = -1 if direction == 1 else 1                         # puts: lower strikes are OTM, calls: higher
    sell_otm = int(cfg.get("sell_otm_points", 50) or 0)
    sell_rule = cfg.get("atm_sell_strike_rule", "any")
    buy_rule = cfg.get("atm_buy_strike_rule", "any")
    by_k = {q.strike: q for q in quotes}
    # hedge: the strike with the required ending nearest to ATM -/+ hedge_otm_points; on a tie the one nearer ATM
    # (e.g. ATM 25900, 200 OTM, ending 50 -> 25750, not 25650, so the spread stays 150-200 wide)
    target = atm + otm * float(cfg.get("hedge_otm_points", 200) or 0)
    tol = 0 if _strike_ok(target, buy_rule) else step         # exact strike when it already has the right ending
    hedges = [q for q in quotes if _strike_ok(q.strike, buy_rule) and abs(q.strike - target) <= tol]
    if not hedges:
        return []
    hedge = min(hedges, key=lambda q: (abs(q.strike - target), abs(q.strike - atm)))
    sb = band(cfg, *ATM_SELL_BAND)
    sells = [by_k[k] for k in (atm + otm * x for x in range(0, sell_otm + 1, step))
             if k in by_k and _strike_ok(k, sell_rule)]
    return [HedgePair(direction, hedge, s, ref_price, atm) for s in sells
            if _in_band(s.ltp, *sb) and (s.strike - hedge.strike) * otm < 0]


def find_pair(direction: int, chain: List[OptionQuote], ref_price: float, cfg) -> Tuple[Optional[HedgePair], List[HedgePair]]:
    step = int(cfg.get("strike_step", 50))
    width = int(cfg.get("strike_scan_width", 10))
    min_prem = float(cfg.get("min_leg_premium", 0) or 0)
    max_spread = float(cfg.get("max_bid_ask_spread", 0) or 0)
    atm, window = strike_window(ref_price, step, width)
    wset = set(window)

    opt = "PE" if direction == 1 else "CE"
    quotes = [q for q in chain if q.opt_type == opt and q.strike in wset and q.ltp >= min_prem
              and _spread_ok(q, max_spread)]
    if cfg.get("strike_mode", "premium_band") == "atm_offset":
        matches = _atm_pairs(direction, quotes, ref_price, atm, step, cfg)
    else:
        matches = _premium_pairs(direction, quotes, ref_price, atm, cfg)
    matches.sort(key=lambda p: (-p.sell.ltp, abs(p.buy.strike - p.sell.strike)))
    if not matches:
        return None, []
    best = matches[0]
    best.candidates = len(matches)
    return best, matches


def _premium_pairs(direction, quotes, ref_price, atm, cfg) -> List[HedgePair]:
    buy_rule = cfg.get("buy_strike_rule", "ends_with_50")
    sell_rule = cfg.get("sell_strike_rule", "ends_with_50")
    sell_band, diff_range = band(cfg, *SELL_BAND), band(cfg, *DIFF_RANGE)
    sells = [q for q in quotes if _strike_ok(q.strike, sell_rule) and _in_band(q.ltp, *sell_band)]
    buys = [q for q in quotes if _strike_ok(q.strike, buy_rule)]
    matches: List[HedgePair] = []
    for s in sells:
        for b in buys:
            beyond = b.strike < s.strike if direction == 1 else b.strike > s.strike   # hedge is further out of the money
            if beyond and _in_band(round(s.ltp - b.ltp, 2), *diff_range):
                matches.append(HedgePair(direction, b, s, ref_price, atm))
    return matches
