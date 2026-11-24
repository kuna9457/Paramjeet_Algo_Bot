"""Telegram message texts for the trade lifecycle (signal -> orders -> TP1 -> SL trail -> exit).

Pure functions: the live runner and the dashboard's "send sample BUY / SELL messages" test use the very same
formatters, so what you see in the test is exactly what a real trade sends.
"""
from __future__ import annotations

from typing import Iterable, Optional

TRIGGER_TEXT = {
    "IDM": "IDM - inducement sweep, price reversed",
    "OB": "OB - retest of an Order Block",
    "FVG": "FVG - retest of a Fair Value Gap",
    "CHoCH": "CHoCH - retest after Change of Character",
    "CHoCH+IDM": "CHoCH + IDM - structure change with inducement sweep",
}


def _f(x, nd=2) -> str:
    return "-" if x is None else f"{float(x):,.{nd}f}"


def _rs(x) -> str:
    return f"Rs {float(x):+,.0f}"


def trigger_text(trigger: str) -> str:
    return TRIGGER_TEXT.get(str(trigger), str(trigger) or "-")


def signal_msg(tid: str, ev, logic: str = "", late: bool = False) -> str:
    buy = ev.direction == 1
    side = "BUY" if buy else "SELL"
    sl_pts = abs(ev.price - ev.sl) if ev.price is not None and ev.sl is not None else None
    tgt_pts = abs(ev.target - ev.price) if ev.price is not None and ev.target is not None else None
    lines = [f"{'🟢' if buy else '🔴'} {side} SIGNAL  [{tid}]",
             f"Trigger: {trigger_text(ev.trigger)}",
             f"Nifty entry: {_f(ev.price)}",
             f"Stop-loss: {_f(ev.sl)}" + (f" ({sl_pts:.0f} pts)" if sl_pts is not None else ""),
             f"TP1: {_f(ev.tp1)}  (stop moves to entry after TP1)",
             f"Target: {_f(ev.target)}" + (f" ({tgt_pts:.0f} pts)" if tgt_pts is not None else "") + f"  | R:R {_f(ev.rr)}"]
    if logic:
        lines.append(f"Logic: {logic}")
    if late:
        lines.append("(late entry after restart)")
    return "\n".join(lines)


def pair_msg(tid: str, describe: str, expiry, candidates: int) -> str:
    return (f"🔎 [{tid}] Option pair chosen ({candidates} matches), expiry {expiry}\n{describe}")


def leg_lines(name: str, leg: dict) -> list:
    b, s = leg.get("buy") or {}, leg.get("sell") or {}
    out = [f"{name}: {leg.get('status')}"]
    out.append(f"  BUY  {b.get('symbol', '-')}  qty {b.get('qty', 0)}  @ {_f(b.get('entry'))}")
    out.append(f"  SELL {s.get('symbol', '-')}  qty {s.get('qty', 0)}  @ {_f(s.get('entry'))}")
    net, net_rs = leg.get("net_entry"), leg.get("net_entry_rs")
    if net is not None:
        out.append(f"  Net premium {'received' if net >= 0 else 'paid'} {abs(net):.2f}/unit = Rs {abs(net_rs or 0):,.0f} total")
    if leg.get("msg"):
        out.append(f"  note: {leg['msg'].strip()}")
    return out


def combo_open_msg(tid: str, combo: dict) -> str:
    ok = combo.get("status") == "OPEN"
    lines = [f"{'✅ ORDERS PLACED' if ok else '❌ ORDERS FAILED'}  [{tid}]  {combo.get('side', '')} signal",
             f"Combo {combo.get('id')} | expiry {combo.get('expiry')}"]
    for name, leg in (combo.get("legs") or {}).items():
        lines += leg_lines(name, leg)
    lines.append(f"Nifty entry {_f(combo.get('nifty_entry'))} | SL {_f(combo.get('sl'))} | TP1 {_f(combo.get('tp1'))} | "
                 f"Target {_f(combo.get('target'))}")
    return "\n".join(lines)


def tp1_msg(tid: str, ev, entry: Optional[float], sl_now: Optional[float]) -> str:
    return (f"🎯 TP1 REACHED  [{tid}]\nNifty touched {_f(ev.price)}.\n"
            f"Stop-loss is now {_f(sl_now)}; it trails to entry {_f(entry)} from the next candle.")


def sl_move_msg(tid: str, old_sl: Optional[float], new_sl: Optional[float], entry: Optional[float]) -> str:
    return (f"🛡 STOP-LOSS TRAILED  [{tid}]\nSL moved {_f(old_sl)}  ->  {_f(new_sl)}"
            + (" (entry = break-even)" if entry is not None and new_sl == entry else ""))


def exit_msg(tid: str, ev) -> str:
    return (f"{'🟢' if ev.direction == 1 else '🔴'} STRATEGY EXIT  [{tid}]\nReason: {ev.reason} | Nifty {_f(ev.price)} | "
            f"{(ev.pnl_points or 0):+.2f} pts\nClosing the option legs ...")


def combo_closed_msg(tid: str, combo: dict, reason: str) -> str:
    ok = combo.get("status") == "CLOSED"
    lines = [f"{'✅ POSITION CLOSED' if ok else '🚨 CLOSE INCOMPLETE'}  [{tid}]  reason {reason}"]
    total = 0.0
    for name, leg in (combo.get("legs") or {}).items():
        b, s = leg.get("buy") or {}, leg.get("sell") or {}
        lines.append(f"{name}: {leg.get('status')}")
        lines.append(f"  BUY  {b.get('symbol', '-')}: {_f(b.get('entry'))} -> {_f(b.get('exit'))}")
        lines.append(f"  SELL {s.get('symbol', '-')}: {_f(s.get('entry'))} -> {_f(s.get('exit'))}")
        pnl = (combo.get("pnl") or {}).get(name, leg.get("pnl", 0)) or 0
        total += pnl
        lines.append(f"  P&L {_rs(pnl)}")
    lines.append(f"Total P&L {_rs(total)}")
    if not ok:
        lines.append("EXIT INCOMPLETE - check broker positions manually NOW")
    return "\n".join(lines)


def open_positions_lines(combos: Iterable[dict], status_line) -> str:
    return "\n".join(status_line(c) for c in combos)
