"""Dry-run of the Telegram flow: builds the exact messages a BUY or SELL trade would send. Places NO orders.

With a broker connection (`data`) it uses the real option chain, your real hedge settings and the broker's real lot size,
so the test shows which legs would really be punched right now. Without one it uses clearly marked sample numbers.
"""
from __future__ import annotations

import math
from datetime import date
from typing import List, Optional

from ..brokers.base import Instrument, OptionQuote
from ..hedge.expiry import expiry_for
from ..hedge.scanner import find_pair, range_text, strike_window
from ..strategy.engine import Event
from . import messages as M

SAMPLE_LOT = 65   # current NIFTY lot size (the real bot uses the exchange value from the broker)


def _sample_chain(ref: float, expiry: date, lot: int) -> List[OptionQuote]:
    """Believable premiums around `ref` (only used when no broker is connected)."""
    out = []
    atm = round(ref / 50) * 50
    for i in range(-10, 11):
        k = float(atm + i * 50)
        tv = 140.0 * math.exp(-abs(k - atm) / 350)   # time value fades away from ATM
        ce = max(atm - k, 0) + tv
        pe = max(k - atm, 0) + tv
        for t, px in (("CE", ce), ("PE", pe)):
            px = round(max(px, 5.0), 2)
            out.append(OptionQuote(k, t, px,
                                   Instrument(f"SAMPLE:{int(k)}{t}", f"NIFTY{expiry:%d%b%y}{int(k)}{t}".upper(), expiry, k, t, lot),
                                   round(px - 0.05, 2), round(px + 0.05, 2)))
    return out


def simulate(side: str, S, data=None, notify_prefix: str = "🧪 TEST - no order was placed\n") -> List[str]:
    """Messages for one full trade: signal, pair, orders placed, TP1, SL trail, strategy exit, position closed."""
    buy = side.upper() == "BUY"
    d = 1 if buy else -1
    H = S.hedge
    today = date.today()
    sample = data is None
    if sample:
        ref, expiry, lot = 23500.0, date.fromordinal(today.toordinal() + 8), SAMPLE_LOT
        chain = _sample_chain(ref, expiry, lot)
    else:
        expiry, _ = expiry_for(data.option_expiries(), H, today)
        inst = data.signal_instrument(S.bot.signal_symbol)
        ref = data.ltp([inst]).get(inst.key)
        _, strikes = strike_window(ref, int(H.strike_step), int(H.strike_scan_width))
        chain = data.option_chain(expiry, strikes)
    pair, matches = find_pair(d, chain, ref, H)
    sl_pts = 80.0
    sl_mode_pts = {"50 Points": 50, "60 Points": 60, "70 Points": 70, "80 Points": 80, "90 Points": 90,
                   "100 Points": 100}.get(str(S.strategy.slFixedPreset))
    if sl_mode_pts and S.strategy.slMode.startswith("Fixed"):
        sl_pts = float(sl_mode_pts)
    elif S.strategy.slMode.startswith("ATR"):
        sl_pts = 90.0                                   # sample: 3.0 x ATR on a normal-volatility day
    rr = max(1.5 if S.strategy.strategy_version != "v9.2" else 1.25, float(S.strategy.minTargetRr))
    tp1_r = float(S.strategy.tp1RrMultiple) if S.strategy.strategy_version == "v9.2" else 1.0
    px = round(ref, 2)
    ev = Event("ENTRY", 1, f"{today.isoformat()}T10:15:00+05:30", d, "OB" if buy else "FVG", px,
               px - d * sl_pts, px + d * sl_pts * rr, px + d * sl_pts * tp1_r, rr)
    tid = _tid(ev)
    out = [M.signal_msg(tid, ev, str(S.strategy.strategy_version))]
    if not pair:
        out.append(f"⚠ [{tid}] No option pair found for the current rules ({range_text(H)}) - the real bot would SKIP this "
                   f"trade and tell you why. Nothing more to show.")
        return [notify_prefix + ("(sample numbers) " if sample else "") + m for m in out]
    out.append(M.pair_msg(tid, pair.describe(), expiry, pair.candidates))
    legs = {}
    brokers = list(S.brokers.enabled or ["paper"])
    for name in brokers:
        lots = int((S.brokers.get("lots") or {}).get(name, 1))
        lot_size = pair.buy.instrument.lot_size or SAMPLE_LOT
        qty = lots * lot_size
        legs[f"paper-{name}"] = {
            "status": "OPEN",
            "buy": {"symbol": pair.buy.instrument.symbol or f"{int(pair.buy.strike)} {pair.buy.opt_type}", "qty": qty,
                    "entry": pair.buy.ltp},
            "sell": {"symbol": pair.sell.instrument.symbol or f"{int(pair.sell.strike)} {pair.sell.opt_type}", "qty": qty,
                     "entry": pair.sell.ltp},
            "net_entry": round(pair.sell.ltp - pair.buy.ltp, 2),
            "net_entry_rs": round((pair.sell.ltp - pair.buy.ltp) * qty, 2)}
    combo = {"id": "TEST-" + tid, "status": "OPEN", "side": side.upper(), "expiry": expiry.isoformat(),
             "nifty_entry": ev.price, "sl": ev.sl, "tp1": ev.tp1, "target": ev.target, "legs": legs}
    out.append(M.combo_open_msg(tid, combo))
    out.append(M.tp1_msg(tid, Event("TP1", 2, "", d, price=ev.tp1), ev.price, ev.sl))
    out.append(M.sl_move_msg(tid, ev.sl, ev.price, ev.price))
    out.append(M.exit_msg(tid, Event("EXIT", 5, "", d, price=ev.target, reason="TARGET",
                                     pnl_points=abs(ev.target - ev.price))))
    pnl = {}
    for name, leg in legs.items():       # illustrative exit at target: sold option decays 20 pts, hedge decays 10 pts
        leg["status"] = "CLOSED"
        leg["buy"]["exit"] = round(max(leg["buy"]["entry"] - 10, 0.05), 2)
        leg["sell"]["exit"] = round(max(leg["sell"]["entry"] - 20, 0.05), 2)
        pnl[name] = round((leg["buy"]["exit"] - leg["buy"]["entry"]) * leg["buy"]["qty"]
                          + (leg["sell"]["entry"] - leg["sell"]["exit"]) * leg["sell"]["qty"], 2)
    combo.update(status="CLOSED", pnl=pnl)
    out.append(M.combo_closed_msg(tid, combo, "TARGET") + "\n(exit prices in this test are illustrative)")
    tag = notify_prefix + ("(sample numbers) " if sample else "(real option prices now) ")
    return [tag + m for m in out]


def _tid(ev) -> str:
    from ..live.runner import trade_id
    return trade_id(ev)


def simulate_text(side: str, S, data=None) -> str:
    """The whole BUY or SELL flow as ONE Telegram message (header once, steps separated by a line)."""
    head = "🧪 TEST - no order was placed " + ("(sample numbers)" if data is None else "(real option prices now)")
    steps = simulate(side, S, data, "")
    steps = [m.replace("(sample numbers) ", "").replace("(real option prices now) ", "") for m in steps]
    return head + "\n\n" + "\n\n- - - - - - - -\n\n".join(steps)
