"""Plotly figures for the dashboard: strategy chart with trades, equity curve, daily P&L."""
from __future__ import annotations

from datetime import timedelta
from typing import List, Optional

import pandas as pd
import plotly.graph_objects as go

UP, DOWN = "#1a9e5c", "#d64545"          # candles / BUY / SELL (market convention)
SL_C, TG_C, EN_C = "#d64545", "#1a9e5c", "#6b7280"
SH_C, SLW_C, EQ_C = "#b45309", "#2563eb", "#9ca3af"


def _ts(x) -> Optional[pd.Timestamp]:
    if x in (None, "", "None"):
        return None
    t = pd.Timestamp(x)
    return t.tz_localize(None) if t.tzinfo is not None else t


def bars_frame(bars: List[dict]) -> pd.DataFrame:
    """bars: dicts with t,o,h,l,c (+ optional sh, sl, eq, trend)."""
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    df["t"] = pd.to_datetime(df["t"].astype(str), utc=False)
    if getattr(df["t"].dt, "tz", None) is not None:
        df["t"] = df["t"].dt.tz_localize(None)
    return df


def _rangebreaks(df: pd.DataFrame) -> list:
    days = set(df["t"].dt.normalize())
    if not days:
        return []
    allw = pd.bdate_range(min(days), max(days))
    holidays = [d.strftime("%Y-%m-%d") for d in allw if d not in days]
    rb = [dict(bounds=["sat", "mon"]), dict(bounds=[15.5, 9.25], pattern="hour")]
    if holidays:
        rb.append(dict(values=holidays))
    return rb


def price_chart(df: pd.DataFrame, trades: List[dict], tf: int, show_levels: bool = True,
                show_sltp: bool = True, position: Optional[dict] = None, height: int = 650,
                xrange: Optional[tuple] = None, title: str = "") -> go.Figure:
    """trades: dicts with direction, trigger, entry_time, entry, sl, target, exit_time, exit, reason, pnl_points."""
    fig = go.Figure()
    if df.empty:
        return fig
    fig.add_trace(go.Candlestick(x=df["t"], open=df["o"], high=df["h"], low=df["l"], close=df["c"], name="Nifty",
                                 increasing_line_color=UP, decreasing_line_color=DOWN,
                                 increasing_fillcolor=UP, decreasing_fillcolor=DOWN, line_width=1))
    if show_levels:
        for col, name, color in (("sh", "Protected strong high", SH_C), ("sl", "Protected strong low", SLW_C),
                                 ("eq", "Equilibrium (50%)", EQ_C)):
            if col in df and df[col].notna().any():
                fig.add_trace(go.Scatter(x=df["t"], y=df[col], mode="lines", name=name, line_shape="hv",
                                         line=dict(color=color, width=1.2, dash="dot" if col == "eq" else "solid"),
                                         hovertemplate=name + ": %{y:.2f}<extra></extra>", visible=True))

    bar = timedelta(minutes=tf)
    last_t = df["t"].iloc[-1].to_pydatetime()
    sl_x, sl_y, tg_x, tg_y, path_x, path_y = [], [], [], [], [], []
    for side, sym, color in ((1, "triangle-up", UP), (-1, "triangle-down", DOWN)):
        tt = [t for t in trades if t["direction"] == side]
        if not tt:
            continue
        fig.add_trace(go.Scatter(
            x=[_ts(t["entry_time"]) for t in tt], y=[t["entry"] for t in tt], mode="markers",
            name="BUY entry" if side == 1 else "SELL entry",
            marker=dict(symbol=sym, size=13, color=color, line=dict(color="white", width=1.5)),
            customdata=[[t["trigger"], t["sl"], t["target"], t.get("rr") or 0] for t in tt],
            hovertemplate=("<b>" + ("BUY" if side == 1 else "SELL") + " %{customdata[0]}</b><br>%{x}<br>"
                           "Entry %{y:.2f}<br>SL %{customdata[1]:.2f}<br>Target %{customdata[2]:.2f}<br>"
                           "R:R %{customdata[3]:.2f}<extra></extra>")))
    closed = [t for t in trades if t.get("exit_time")]
    if closed:
        fig.add_trace(go.Scatter(
            x=[_ts(t["exit_time"]) for t in closed], y=[t["exit"] for t in closed], mode="markers", name="Exit",
            marker=dict(symbol="x", size=10, line=dict(width=2),
                        color=[UP if (t.get("pnl_points") or 0) > 0 else DOWN for t in closed]),
            customdata=[[t["reason"], t.get("pnl_points") or 0, t.get("net_r") or 0] for t in closed],
            hovertemplate=("<b>Exit %{customdata[0]}</b><br>%{x}<br>Price %{y:.2f}<br>"
                           "Points %{customdata[1]:+.2f}<br>Net R %{customdata[2]:+.2f}<extra></extra>")))
    for t in trades:
        a = _ts(t["entry_time"])
        b = _ts(t.get("exit_time")) or last_t
        if a is None:
            continue
        if b <= a:
            b = a + bar
        sl_x += [a, b, None]; sl_y += [t["sl"], t["sl"], None]
        tg_x += [a, b, None]; tg_y += [t["target"], t["target"], None]
        if t.get("exit_time"):
            path_x += [a, b, None]; path_y += [t["entry"], t["exit"], None]
    if show_sltp and trades:
        fig.add_trace(go.Scatter(x=sl_x, y=sl_y, mode="lines", name="Stop loss",
                                 line=dict(color=SL_C, width=1.5, dash="dash"), hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=tg_x, y=tg_y, mode="lines", name="Target",
                                 line=dict(color=TG_C, width=1.5, dash="dash"), hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=path_x, y=path_y, mode="lines", name="Entry → exit",
                                 line=dict(color=EN_C, width=1, dash="dot"), hoverinfo="skip"))
    if position and position.get("dir"):
        for key, name, color in (("entry", "Entry", EN_C), ("sl", "SL", SL_C), ("target", "Target", TG_C),
                                 ("tp1", "TP1", "#7c3aed")):
            v = position.get(key)
            if v:
                fig.add_hline(y=v, line=dict(color=color, width=1.5, dash="dash"),
                              annotation_text=f"{name} {v:.2f}", annotation_position="right")
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=40 if title else 10, b=10),
        xaxis_rangeslider_visible=False, hovermode="x unified" if len(df) < 600 else "closest",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0), dragmode="pan",
        yaxis=dict(title="Nifty", side="right", fixedrange=False),
    )
    if title:
        fig.update_layout(title=dict(text=title, font=dict(size=14)))
    fig.update_xaxes(rangebreaks=_rangebreaks(df), showspikes=True, spikemode="across", spikethickness=1)
    if xrange:
        fig.update_xaxes(range=list(xrange))
        sub = df[(df["t"] >= xrange[0]) & (df["t"] <= xrange[1])]
        if not sub.empty:
            lo, hi = sub["l"].min(), sub["h"].max()
            for t in trades:
                a = _ts(t["entry_time"])
                if a is not None and xrange[0] <= a <= xrange[1]:
                    lo, hi = min(lo, t["sl"], t["target"]), max(hi, t["sl"], t["target"])
            pad = (hi - lo) * 0.05
            fig.update_yaxes(range=[lo - pad, hi + pad])
    return fig


def equity_chart(trades: List[dict], value_key: str = "net_r", label: str = "Cumulative net R") -> go.Figure:
    closed = [t for t in trades if t.get("exit_time")]
    fig = go.Figure()
    if not closed:
        return fig
    df = pd.DataFrame({"t": [_ts(t["exit_time"]) for t in closed], "v": [t.get(value_key) or 0 for t in closed]})
    df = df.sort_values("t")
    df["cum"] = df["v"].cumsum()
    df["peak"] = df["cum"].cummax()
    fig.add_trace(go.Scatter(x=df["t"], y=df["cum"], mode="lines", name=label, line=dict(color="#2563eb", width=2),
                             fill="tozeroy", fillcolor="rgba(37,99,235,0.08)",
                             hovertemplate="%{x}<br>" + label + ": %{y:,.2f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=df["t"], y=df["peak"], mode="lines", name="Peak", line=dict(color="#9ca3af", width=1, dash="dot"),
                             hoverinfo="skip"))
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), showlegend=False, hovermode="x unified")
    return fig


def daily_pnl_chart(daily: pd.DataFrame, col: str = "pnl") -> go.Figure:
    fig = go.Figure()
    if daily.empty:
        return fig
    fig.add_trace(go.Bar(x=daily.index.astype(str), y=daily[col], name="Daily P&L",
                         marker_color=[UP if v >= 0 else DOWN for v in daily[col]],
                         hovertemplate="%{x}<br>₹%{y:,.0f}<extra></extra>"))
    fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), showlegend=False)
    fig.update_xaxes(type="category")
    return fig


def max_drawdown(values: List[float]) -> float:
    peak = cum = dd = 0.0
    for v in values:
        cum += v
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def trade_dicts(trades) -> List[dict]:
    out = []
    for t in trades:
        d = t if isinstance(t, dict) else t.__dict__
        out.append(dict(d))
    return out


def window_for(trade: dict, tf: int, before: int = 60, after: int = 30):
    a = _ts(trade["entry_time"])
    b = _ts(trade.get("exit_time")) or a
    # count session minutes roughly: pad in bar units but at least one session
    pad_b = timedelta(minutes=tf * before)
    pad_a = timedelta(minutes=tf * after)
    return (a - pad_b, b + pad_a)


__all__ = ["bars_frame", "price_chart", "equity_chart", "daily_pnl_chart", "max_drawdown", "trade_dicts",
           "window_for"]
