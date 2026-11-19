"""Joins strategy signals with what the bot did about them (decision log + option combo + broker fills).

Pure functions, no Streamlit - used by the dashboard and covered by tests.
Old data (before the decision log existed) still works: the combo is matched by signal time + direction.
"""
from __future__ import annotations

from typing import Dict, List, Optional

SIDE = {1: "BUY", -1: "SELL"}


def action_id(ev: dict) -> str:
    return f"{ev.get('time')}|{ev.get('direction')}|{ev.get('trigger')}|{ev.get('mode', '')}"


def _first_leg(combo: dict) -> dict:
    legs = combo.get("legs") or {}
    for leg in legs.values():
        if (leg.get("buy") or {}).get("entry"):
            return leg
    return next(iter(legs.values()), {}) if legs else {}


def _price(scan, fill) -> str:
    if fill:
        return f"{scan:.2f} → {fill:.2f}" if scan else f"{fill:.2f}"
    return f"{scan:.2f}" if scan else ""


def _net_text(net: Optional[float]) -> str:
    if net is None:
        return ""
    return f"{'CREDIT' if net >= 0 else 'DEBIT'} {abs(net):.2f}"


def find_combo(ev: dict, combos: List[dict], action: Optional[dict]) -> Optional[dict]:
    cid = (action or {}).get("combo_id")
    if cid:
        for c in combos:
            if c.get("id") == cid:
                return c
    for c in combos:
        if (c.get("signal_time") == ev.get("time") and c.get("direction") == ev.get("direction")
                and (c.get("trigger") or "") == (ev.get("trigger") or "")):
            return c
    return None


def combo_total(combo: dict) -> Optional[float]:
    pnl = combo.get("pnl") or {}
    return round(sum(v or 0 for v in pnl.values()), 2) if pnl else None


def signal_rows(events: List[dict], combos: List[dict], actions: List[dict]) -> List[dict]:
    """One row per ENTRY signal, newest first, with the option trade that followed (if any)."""
    by_id = {a.get("id"): a for a in actions}
    rows = []
    for ev in sorted((e for e in events if e.get("kind") == "ENTRY"), key=lambda x: str(x.get("time")), reverse=True):
        act = by_id.get(action_id(ev))
        combo = find_combo(ev, combos, act)
        row = {
            "time": str(ev.get("time", ""))[:19].replace("T", " "), "signal": SIDE.get(ev.get("direction"), ""),
            "trigger": ev.get("trigger"), "Nifty": ev.get("price"), "SL": ev.get("sl"), "target": ev.get("target"),
            "R:R": None if ev.get("rr") is None else round(ev["rr"], 2), "mode": ev.get("mode"),
        }
        if combo:
            leg = _first_leg(combo)
            b, s = combo.get("buy") or {}, combo.get("sell") or {}
            lb, ls = leg.get("buy") or {}, leg.get("sell") or {}
            net = leg.get("net_entry")
            if net is None and lb.get("entry") and ls.get("entry"):
                net = round(ls["entry"] - lb["entry"], 2)
            row.update({
                "action": "TRADED" if combo.get("status") in ("OPEN", "CLOSED", "CLOSE_FAILED") else combo.get("status"),
                "why / note": (act or {}).get("reason", ""), "expiry": combo.get("expiry"),
                "BUY leg": f"BUY {int(b['strike'])} {b['type']}" if b.get("strike") else "",
                "BUY premium (scan → fill)": _price(b.get("scan_ltp"), lb.get("entry")),
                "SELL leg": f"SELL {int(s['strike'])} {s['type']}" if s.get("strike") else "",
                "SELL premium (scan → fill)": _price(s.get("scan_ltp"), ls.get("entry")),
                "net premium": _net_text(net if net is not None else combo.get("scan_net")),
                "pair mode": combo.get("pair_mode", "debit"), "qty": lb.get("qty") or ls.get("qty"),
                "combo status": combo.get("status"), "exit reason": combo.get("exit_reason", ""),
                "closed": str(combo.get("closed_at") or "")[:19].replace("T", " "),
                "P&L ₹": combo_total(combo), "combo id": combo.get("id"),
            })
        else:
            row.update({"action": (act or {}).get("outcome") or "NOT TRADED / not recorded",
                        "why / note": (act or {}).get("reason", ""), "expiry": (act or {}).get("expiry", ""),
                        "combo id": (act or {}).get("combo_id", "")})
        rows.append(row)
    return rows


def leg_rows(combo: dict) -> List[dict]:
    """Per-broker detail of one combo: what was bought / sold, at what price, exit and P&L."""
    out = []
    b, s = combo.get("buy") or {}, combo.get("sell") or {}
    for broker, leg in (combo.get("legs") or {}).items():
        lb, ls = leg.get("buy") or {}, leg.get("sell") or {}
        net = leg.get("net_entry")
        out.append({
            "broker": broker, "status": leg.get("status"),
            "BUY symbol": lb.get("symbol"), "BUY qty": lb.get("qty"), "BUY scan": b.get("scan_ltp"),
            "BUY entry": lb.get("entry"), "BUY exit": lb.get("exit"),
            "SELL symbol": ls.get("symbol"), "SELL qty": ls.get("qty"), "SELL scan": s.get("scan_ltp"),
            "SELL entry": ls.get("entry"), "SELL exit": ls.get("exit"),
            "net at entry (per unit)": _net_text(net), "net at entry ₹": leg.get("net_entry_rs"),
            "P&L ₹": leg.get("pnl"), "note": (leg.get("msg") or "").strip(),
        })
    return out


def timeline(combo: dict, ev: dict, action: Optional[dict]) -> List[str]:
    """Human-readable story of one signal."""
    lines = [f"Signal {SIDE.get(ev.get('direction'), '')} via {ev.get('trigger')} at Nifty {ev.get('price')} "
             f"(SL {ev.get('sl')}, target {ev.get('target')}, R:R {ev.get('rr')})"]
    if action and action.get("wanted"):
        lines.append(f"Looked for: {action['wanted']} · expiry {action.get('expiry')} · "
                     f"{action.get('scan_rounds', 1)} scan(s) · {action.get('candidates', 0)} matching pair(s)")
    if action and action.get("reason"):
        lines.append(f"{action.get('outcome', '')}: {action['reason']}")
    if combo:
        lines.append(f"Combo {combo.get('id')} opened {str(combo.get('opened_at', ''))[:19]} · "
                     f"{combo.get('pair_mode', 'debit')} pair · status {combo.get('status')}")
        if combo.get("exit_reason"):
            lines.append(f"Closed {str(combo.get('closed_at', ''))[:19]} · reason {combo['exit_reason']} · "
                         f"P&L ₹{combo_total(combo)}")
    return lines


def summarize(rows: List[dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        out[r["action"]] = out.get(r["action"], 0) + 1
    return out
