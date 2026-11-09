"""Backtest the strategy engine on CSV (TradingView export) or broker history, with optional parity check.

TradingView export: chart -> "Export chart data" -> CSV with `time,open,high,low,close,...` plus one column
per plot. The indicator's plotshape columns ("BUY CHoCH Badge", "SELL IDM Badge", ...) are compared
bar by bar with the Python signals.
"""
from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from bot.config import StrategySettings
from bot.data.candle import IST, Candle, resample, to_ist
from bot.strategy.engine import NexusEngine


def _parse_time(v: str) -> datetime:
    v = v.strip()
    try:
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        return datetime.fromtimestamp(x, IST)
    except ValueError:
        return to_ist(datetime.fromisoformat(v.replace("Z", "+00:00")))


def load_csv(path: str | Path) -> Tuple[List[Candle], List[Dict[str, str]]]:
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8-sig")))
    if not rows:
        raise ValueError("empty CSV")
    cols = {c.lower().strip(): c for c in rows[0].keys()}
    tcol = cols.get("time") or cols.get("datetime") or cols.get("date") or cols.get("timestamp")
    candles = []
    for r in rows:
        candles.append(Candle(_parse_time(r[tcol]), float(r[cols["open"]]), float(r[cols["high"]]),
                              float(r[cols["low"]]), float(r[cols["close"]]),
                              float(r.get(cols.get("volume", ""), 0) or 0) if cols.get("volume") else 0.0))
    return candles, rows


def detect_tf(candles: List[Candle]) -> int:
    diffs = sorted(int((b.time - a.time).total_seconds() // 60) for a, b in zip(candles, candles[1:])
                   if a.time.date() == b.time.date())
    return diffs[len(diffs) // 2] if diffs else 1


def _flag(v: Optional[str]) -> bool:
    if v is None:
        return False
    v = v.strip()
    if v == "" or v.lower() == "nan":
        return False
    try:
        return float(v) != 0 and not math.isnan(float(v))
    except ValueError:
        return True


def run_backtest(candles: List[Candle], settings: StrategySettings, tv_rows: List[Dict[str, str]] | None = None,
                 out_dir: Path | None = None) -> Dict[str, object]:
    in_tf = detect_tf(candles)
    feed = candles if in_tf == settings.chart_timeframe else resample(candles, settings.chart_timeframe)
    eng = NexusEngine(settings)
    diag_rows = []
    for c in feed:
        eng.on_bar(c)
        diag_rows.append(dict(eng.diag))
    res: Dict[str, object] = {"bars": len(feed), "input_tf": in_tf, "stats": eng.stats(), "trades": eng.trades,
                              "feed": feed, "diag": diag_rows}

    if tv_rows is not None and in_tf == settings.chart_timeframe:
        keys = list(tv_rows[0].keys())
        buy_cols = [k for k in keys if k.upper().startswith("BUY")]
        sell_cols = [k for k in keys if k.upper().startswith("SELL")]
        if buy_cols or sell_cols:
            mism = []
            tv_b = tv_s = py_b = py_s = 0
            for r, d in zip(tv_rows, diag_rows):
                tb = any(_flag(r.get(k)) for k in buy_cols)
                ts = any(_flag(r.get(k)) for k in sell_cols)
                tv_b += tb; tv_s += ts; py_b += d["triggerBuy"]; py_s += d["triggerSell"]
                if tb != d["triggerBuy"] or ts != d["triggerSell"]:
                    mism.append((d["time"], f"TV buy={tb} sell={ts} | PY buy={d['triggerBuy']} sell={d['triggerSell']}"))
            res["parity"] = {"tv_buys": tv_b, "tv_sells": tv_s, "py_buys": py_b, "py_sells": py_s,
                             "mismatched_bars": len(mism), "first_mismatches": mism[:20]}

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "backtest_trades.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["dir", "trigger", "entry_time", "entry", "sl", "target", "tp1", "rr", "exit_time", "exit",
                        "reason", "pnl_points", "net_r"])
            for t in eng.trades:
                w.writerow(["BUY" if t.direction == 1 else "SELL", t.trigger, t.entry_time, t.entry, round(t.sl, 2),
                            round(t.target, 2), round(t.tp1, 2), round(t.rr, 2), t.exit_time, t.exit, t.reason,
                            round(t.pnl_points or 0, 2), round(t.net_r or 0, 3)])
        with open(out_dir / "backtest_bars.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(diag_rows[0].keys()) if diag_rows else [])
            w.writeheader()
            w.writerows(diag_rows)
    return res
