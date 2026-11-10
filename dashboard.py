"""Nexus SMC bot – web dashboard.

    streamlit run dashboard.py

Pages: Bot control (live status/chart/combos, start/stop/kill), Settings, Login & connections,
History, Backtest (strategy on a historical chart), Logs.
"""
from __future__ import annotations

import dataclasses
import hmac
import os
import shutil
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pandas as pd
import streamlit as st
from dotenv import load_dotenv, set_key

from bot.config import DEFAULT_SETTINGS, ROOT, env, load_settings
from bot.data.candle import now_ist
from bot.services.notifier import Notifier
from bot.services.storage import LocalStorage, make_storage
from bot.services.tokens import ENV_KEYS, save_token, token_day
from bot.ui import charts, process, settings_io

st.set_page_config(page_title="Nexus SMC Bot", page_icon="📈", layout="wide")
load_dotenv(ROOT / ".env", override=True)
ON_RENDER = bool(os.getenv("RENDER"))   # set automatically by Render

# ====================================================================================== helpers
PINE_TITLES = {
    "strat1_IDM": "Enable IDM Pullback Trigger", "idmTf": "IDM Sweep Timeframe",
    "idmLookback": "IDM Minor Pivot Lookback (Bars)", "strat6_IDM_Precision": "Require Strict OB/FVG Confluence for IDM",
    "strat8_HTF_IDM_Trap": "HTF IDM Trap Filter", "strat2_CHOCH": "Enable CHoCH Reversal Trigger",
    "chochStructureTf": "CHoCH Structure Timeframe", "strat9_Fake_CHOCH": "Fake Breakout Filter (Body Close Required)",
    "htfSwingLength": "CHoCH Structure Pivot Lookback", "swingLength": "Local Chart Pivot Lookback (If Chart TF)",
    "strat3_OB": "Enable Order Block (OB) Trigger", "obTimeframe": "Order Block Timeframe (blank = chart TF)",
    "obMitigation": "OB Mitigation Style", "maxOrderBlocks": "Max Active OB Boxes Stored",
    "strat4_FVG": "Enable Fair Value Gap (FVG) Trigger", "maxFvgCount": "Max Active FVGs Stored",
    "strat5_MTF": "Enable HTF Trend Filter (60m/15m Alignment)", "htfTimeframe": "HTF Macro Trend Timeframe",
    "strat7_PD_Filter": "Premium & Discount Filter (Strict 50% Rule)",
    "strat12_OB_Confirm": "Candlestick Confirmation (Rejection Wick + HH/LL)",
    "strat13_Psychology1": "Anti-FOMO Opposing Zone Barrier",
    "strat10_Momentum_CISD": "Displacement Filter (Body >= Min % of Range)", "minDisplacementPct": "Min Body Displacement %",
    "strat11_3Condition": "3-Condition Mandatory Lock", "strat16_POI_Confluence": "Master POI Confluence Mode",
    "filterEmaSqueeze": "Filter EMA Compression / Squeeze", "enableChopFilter": "Enable Choppiness Index Filter",
    "chopIndexMax": "Max Chop Index Ceiling", "enableAdxFilter": "Enable ADX Trend Strength Filter",
    "adxThreshold": "ADX Strength Threshold", "opposingZoneBufferAtr": "Opposing Zone Buffer Distance (ATR)",
    "signalCooldownBars": "Signal Cooldown Bars", "filterDuplicateTrades": "Wait for Active Trade to Close Before New Entry",
    "allowMultiTriggerEntries": "Allow Multi-Trigger Pyramiding (CHoCH + IDM)", "slMode": "Stop Loss Mode",
    "slFixedPreset": "Fixed SL Preset", "slCustomPoints": "Custom SL Points (If Custom Selected)",
    "slBufferPoints": "SL Safety Buffer Points", "strat14_Psychology2": "Pre-Sweep SL Protection (Behind Sweep Extreme)",
    "maxAllowedSlPoints": "Maximum Allowed SL Distance (Points)", "minTargetRr": "Minimum Target R:R Multiple",
    "subMinTargetAction": "Sub-Minimum Target Action", "enableMaxTargetCap": "Enable Max Target Cap",
    "maxTargetRr": "Max Target R:R Ceiling (If Enabled)", "enableBreakEven": "Move SL to Break-Even at TP1",
    "tp1RrMultiple": "TP1 Milestone R:R (Triggers Break-Even)",
    "strat15_LiquidityRun": "Target Nearest Institutional Swing Liquidity",
    "costPerTradePoints": "Estimated Cost / Slippage (Points per Trade)", "maxTradeDuration": "Max Swing Hold Duration (Bars)",
    "fix_ob_mitigation": "Fix Pine OB-mitigation quirk (false = identical to TradingView)",
}
TF_OPTS = ["", "1", "3", "5", "10", "15", "30", "60", "120", "240"]
CHOICES = {
    "bot.mode": ["paper", "live"], "bot.chart_timeframe": [1, 3, 5, 10, 15, 30, 60],
    "bot.signal_symbol": ["NIFTY_SPOT", "NIFTY_FUT"], "bot.exit_monitoring": ["tick", "candle_close"],
    "brokers.data_source": ["upstox", "zerodha"], "brokers.product": ["NRML", "MIS"],
    "brokers.order_type": ["LIMIT", "MARKET"], "brokers.leg_order": ["buy_first", "simultaneous"],
    "hedge.expiry_mode": ["nearest_weekly", "next_weekly", "current_monthly", "next_monthly", "fixed_date"],
    "hedge.buy_strike_rule": ["any", "ends_with_00", "ends_with_50"],
    "hedge.sell_strike_rule": ["ends_with_50", "ends_with_00", "any"],
    "storage.backend": ["mongo", "local"],
    "strategy.idmTf": TF_OPTS, "strategy.chochStructureTf": TF_OPTS, "strategy.obTimeframe": TF_OPTS,
    "strategy.htfTimeframe": TF_OPTS,
    "strategy.obMitigation": ["Wick Touch", "50% Mean Threshold", "Full Candle Body"],
    "strategy.slMode": ["Fixed Points (Preset)", "Swing High/Low", "Custom Points"],
    "strategy.slFixedPreset": ["70 Points", "80 Points", "90 Points"],
    "strategy.subMinTargetAction": ["Extend to Min R:R", "Skip Trade"],
}
MULTI = {"brokers.enabled": ["upstox", "zerodha"]}
SECTION_TITLES = {"bot": "Bot & chart", "brokers": "Brokers & orders", "hedge": "Options hedge",
                  "safety": "Safety", "alerts": "Alerts", "storage": "Storage", "strategy": "Strategy (Pine inputs)"}


def S():
    return load_settings()


@st.cache_resource(show_spinner="Connecting to storage ...")
def _storage(backend: str, uri: str, db: str):
    return make_storage(backend, uri, db, ROOT / "data_cache")


def storage():
    s = S()
    return _storage(s.storage.get("backend", "mongo"), env("MONGO_URI"), env("MONGO_DB", "nexus_smc_bot"))


def parse_t(x):
    try:
        t = pd.Timestamp(x)
        return t.tz_convert("Asia/Kolkata").tz_localize(None) if t.tzinfo else t
    except Exception:  # noqa
        return None


def combo_mode(c: dict) -> str:
    if c.get("mode"):
        return c["mode"]
    return "paper" if any(str(k).startswith("paper-") for k in (c.get("legs") or {})) else "live"


def combo_total(c: dict) -> float:
    return float(sum((c.get("pnl") or {}).values()) or 0)


def fmt_dir(d) -> str:
    return {1: "🟢 LONG", -1: "🔴 SHORT"}.get(d, "⚪ FLAT")


def fmt_trend(d) -> str:
    return {1: "🟢 Bullish", -1: "🔴 Bearish"}.get(d, "—")


def bot_status():
    st_ = storage()
    doc = st_.find_one("status", {"id": "bot"}) or {}
    proc = process.info()
    age = None
    if doc.get("updated_at"):
        t = parse_t(doc["updated_at"])
        age = (pd.Timestamp(now_ist().replace(tzinfo=None)) - t).total_seconds() if t is not None else None
    alive_hb = age is not None and age < 40 and doc.get("state") not in ("STOPPED", "KILLED")
    running = bool(proc) or alive_hb
    return doc, proc, age, running


def events_to_trades(events: list) -> list:
    trades, cur = [], None
    for e in sorted(events, key=lambda x: str(x.get("time"))):
        k = e.get("kind")
        if k == "ENTRY":
            if cur:
                trades.append(cur)
            cur = {"direction": e.get("direction"), "trigger": e.get("trigger"), "entry_time": e.get("time"),
                   "entry": e.get("price"), "sl": e.get("sl"), "target": e.get("target"), "rr": e.get("rr"),
                   "exit_time": None, "exit": None, "reason": "", "pnl_points": None, "net_r": None}
        elif k == "SL_MOVE" and cur:
            cur["sl"] = e.get("sl")
        elif k == "EXIT" and cur:
            cur.update(exit_time=e.get("time"), exit=e.get("price"), reason=e.get("reason"),
                       pnl_points=e.get("pnl_points"))
            trades.append(cur)
            cur = None
    if cur:
        trades.append(cur)
    return trades


# ====================================================================================== auth
def check_password() -> bool:
    pw = env("DASHBOARD_PASSWORD")
    if not pw or st.session_state.get("auth_ok"):
        return True
    st.title("🔒 Nexus SMC Bot")
    entered = st.text_input("Dashboard password", type="password")
    if st.button("Log in", type="primary"):
        if hmac.compare_digest(entered, pw):
            st.session_state["auth_ok"] = True
            st.rerun()
        st.error("Wrong password")
    return False


# ====================================================================================== page: bot control
def page_control():
    st.title("🤖 Bot control")
    s = S()

    @st.fragment(run_every="5s")
    def live_panel():
        doc, proc, age, running = bot_status()
        state = doc.get("state", "—") if running else "STOPPED"
        c = st.columns(5)
        c[0].metric("Bot", ("🟢 " if running else "🔴 ") + state,
                    help=doc.get("msg") or None)
        c[1].metric("Mode", (doc.get("mode") or (proc or {}).get("mode") or "—").upper())
        c[2].metric(f"{doc.get('signal_symbol', s.bot.signal_symbol)} LTP",
                    f"{doc['ltp']:,.2f}" if doc.get("ltp") else "—")
        c[3].metric("Trend (chart / HTF)", f"{fmt_trend(doc.get('trend'))} / {fmt_trend(doc.get('htf_bias'))}")
        pos = doc.get("position") or {}
        c[4].metric("Strategy position", fmt_dir(pos.get("dir")))
        cap = []
        if age is not None:
            cap.append(f"last heartbeat {age:,.0f}s ago")
        if doc.get("last_bar"):
            cap.append(f"last closed candle {str(doc['last_bar'])[:16].replace('T', ' ')}")
        if doc.get("brokers"):
            cap.append("execution: " + ", ".join(doc["brokers"]))
        if proc:
            cap.append(f"pid {proc['pid']}, started {proc['started_at'][:19].replace('T', ' ')}")
        st.caption(" · ".join(cap) or "Bot has not run yet.")
        if doc.get("state") == "WAITING_LOGIN" and running:
            st.error(f"Waiting for broker login: {doc.get('msg')}. Open **Login & connections**.")
        if process.KILL_FILE.exists():
            st.error("⛔ Kill switch is ON – the bot will square off and stop. Clear it below before starting.")

        if pos.get("dir"):
            p = st.columns(6)
            p[0].metric("Trigger", pos.get("trigger") or "—")
            p[1].metric("Entry", f"{pos.get('entry') or 0:,.2f}")
            p[2].metric("Stop loss", f"{pos.get('sl') or 0:,.2f}")
            p[3].metric("TP1", f"{pos.get('tp1') or 0:,.2f}", "hit" if pos.get("tp1Hit") else None, delta_arrow="off")
            p[4].metric("Target", f"{pos.get('target') or 0:,.2f}")
            p[5].metric("R:R", f"{pos.get('rr') or 0:.2f}")

        st_ = storage()
        combos = st_.find("combos")
        open_c = [x for x in combos if x.get("status") in ("OPEN", "CLOSE_FAILED", "OPENING")]
        today = now_ist().date().isoformat()
        today_c = [x for x in combos if str(x.get("opened_at", "")).startswith(today)]
        realized = sum(combo_total(x) for x in today_c if x.get("status") == "CLOSED")
        mtm_all = doc.get("mtm") or {}
        unreal = sum(sum(v.values()) for v in mtm_all.values()) if mtm_all else 0.0
        k = st.columns(4)
        k[0].metric("Open combos", len(open_c))
        k[1].metric("Today's combos", len(today_c))
        k[2].metric("Realized P&L today", f"₹{realized:,.0f}")
        k[3].metric("Open MTM", f"₹{unreal:,.0f}" if mtm_all else "—")

        if open_c:
            st.subheader("Open combos")
            rows = []
            for x in open_c:
                for bname, leg in (x.get("legs") or {}).items():
                    b, sl = leg.get("buy", {}), leg.get("sell", {})
                    rows.append({"combo": x["id"], "broker": bname, "side": x.get("side"), "status": leg.get("status"),
                                 "BUY": f"{b.get('symbol', '')} x{b.get('qty', 0)} @ {b.get('entry', 0)}",
                                 "SELL": f"{sl.get('symbol', '')} x{sl.get('qty', 0)} @ {sl.get('entry', 0)}",
                                 "expiry": x.get("expiry"), "Nifty SL": x.get("sl"), "Nifty target": x.get("target"),
                                 "MTM ₹": round((mtm_all.get(x["id"]) or {}).get(bname, 0), 0)})
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            if any(x.get("status") == "CLOSE_FAILED" for x in open_c):
                st.error("🚨 A combo exit failed – the bot retries every 30 s. Check broker positions now.")

        chart = st_.find_one("chart", {"id": "live"})
        if chart and chart.get("bars"):
            df = charts.bars_frame(chart["bars"])
            t0 = str(chart["bars"][0]["t"])
            evs = [e for e in st_.find("events") if str(e.get("time", "")) >= t0[:10]]
            trades = events_to_trades(evs)
            start = df["t"].iloc[max(0, len(df) - 150)]
            fig = charts.price_chart(df, trades, int(chart.get("tf", s.bot.chart_timeframe)),
                                     position=pos if pos.get("dir") else None, height=520,
                                     xrange=(start, df["t"].iloc[-1] + timedelta(minutes=30)),
                                     title=f"Live strategy chart – {chart.get('tf')}m (updated "
                                           f"{str(chart.get('updated_at', ''))[11:19]})")
            st.plotly_chart(fig, width="stretch", key="livechart")
        else:
            st.info("The live chart appears after the bot finishes its warm-up.")

    live_panel()

    st.divider()
    st.subheader("Controls")
    doc, proc, age, running = bot_status()
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown("**Start**")
        mode = st.radio("Mode", ["paper", "live"], horizontal=True, index=0 if s.bot.mode != "live" else 1,
                        label_visibility="collapsed")
        confirm = True
        if mode == "live":
            if ON_RENDER:
                st.warning("Render has no fixed outgoing IP, so brokers will reject live orders. Use paper mode here.")
            confirm = st.checkbox("I understand real orders will be placed on " + ", ".join(s.brokers.enabled))
        if st.button("▶ Start bot", type="primary", disabled=running or not confirm or process.KILL_FILE.exists(),
                     width="stretch"):
            try:
                rec = process.start(mode)
                st.success(f"Started ({mode}), pid {rec['pid']}. Warm-up takes ~1 minute.")
                time.sleep(2)
                st.rerun()
            except Exception as e:  # noqa
                st.error(str(e))
    with c2:
        st.markdown("**Stop**")
        st.caption("Graceful stop. Open combos stay open.")
        if st.button("⏹ Stop bot", disabled=not running, width="stretch"):
            process.request_stop()
            st.info("Stop requested – the bot exits within a few seconds.")
        if st.button("Force stop (kill process)", disabled=not proc, width="stretch"):
            process.force_stop()
            st.warning("Process killed.")
            time.sleep(1)
            st.rerun()
    with c3:
        st.markdown("**Kill switch**")
        st.caption("Squares off ALL open combos, then stops the bot.")
        if process.KILL_FILE.exists():
            if st.button("Clear kill switch", width="stretch"):
                process.kill_switch(False)
                st.rerun()
        else:
            sure = st.checkbox("Yes, square off everything")
            if st.button("⛔ KILL – exit all", disabled=not sure, width="stretch"):
                process.kill_switch(True)
                st.error("Kill switch ON.")
    with c4:
        st.markdown("**Quick scan**")
        st.caption("Which option pair would be traded right now (no orders).")
        side = st.radio("Side", ["BUY", "SELL"], horizontal=True, label_visibility="collapsed")
        if st.button("🔎 Scan option chain", width="stretch"):
            scan_now(side)


def scan_now(side: str):
    from bot.hedge.expiry import select_expiry
    from bot.hedge.scanner import find_pair, strike_window
    from bot.live.runner import build_broker
    s = S()
    H = s.hedge
    try:
        with st.spinner("Loading instruments and option chain ..."):
            b = build_broker(s.brokers.data_source, storage())
            b.connect()
            b.load_instruments()
            exp = select_expiry(b.option_expiries(), H.expiry_mode, now_ist().date(), H.get("expiry_date", ""),
                                bool(H.get("use_next_expiry_on_expiry_day", True)))
            inst = b.signal_instrument(s.bot.signal_symbol)
            ref = b.ltp([inst]).get(inst.key)
            _, strikes = strike_window(ref, int(H.strike_step), int(H.strike_scan_width))
            chain = b.option_chain(exp, strikes)
            best, matches = find_pair(1 if side == "BUY" else -1, chain, ref, H)
        st.write(f"Reference {ref} · expiry {exp} · range {H.range_min}-{H.range_max} · {len(chain)} quotes")
        if not matches:
            st.warning("No pair in range right now.")
        for i, p in enumerate(matches[:10]):
            st.write(("✅ " if i == 0 else "· ") + p.describe())
    except Exception as e:  # noqa
        st.error(f"Scan failed: {e}")


# ====================================================================================== page: settings
def _push_settings():
    """Keep a copy of settings.yaml in MongoDB (Render free plan has no disk; render_start.py restores it)."""
    try:
        storage().upsert("config", {"id": "settings"}, {"id": "settings", "saved_at": now_ist().isoformat(),
                                                       "text": DEFAULT_SETTINGS.read_text(encoding="utf-8")})
    except Exception as e:  # noqa
        st.warning(f"Settings saved locally but not backed up to MongoDB: {e}")


def _widget(path, value, comments, prefix="set:"):
    key = ".".join(path)
    name = path[-1]
    label = PINE_TITLES.get(name, name.replace("_", " ").capitalize()) if path[0] == "strategy" else \
        name.replace("_", " ").capitalize()
    if path[0] == "brokers" and path[1] == "lots":
        label = f"Lots on {name}"
    help_ = comments.get(path)
    if path[0] == "strategy":
        help_ = f"`{name}`" + (f" – {help_}" if help_ else "")
    wkey = prefix + key
    if key in MULTI:
        return st.multiselect(label, MULTI[key], default=list(value or []), help=help_, key=wkey)
    if key in CHOICES:
        opts = CHOICES[key]
        v = value if value in opts else (str(value) if str(value) in opts else opts[0])
        return st.selectbox(label, opts, index=opts.index(v), help=help_, key=wkey,
                            format_func=lambda o: "(chart timeframe)" if o == "" else str(o))
    if isinstance(value, bool):
        return st.toggle(label, value=value, help=help_, key=wkey)
    if isinstance(value, int):
        return int(st.number_input(label, value=value, step=1, help=help_, key=wkey))
    if isinstance(value, float):
        step = 0.05 if abs(value) < 5 else (0.5 if abs(value) < 100 else 5.0)
        return float(st.number_input(label, value=value, step=step, help=help_, key=wkey, format="%.2f"))
    return st.text_input(label, value="" if value is None else str(value), help=help_, key=wkey)


def page_settings():
    st.title("⚙️ Settings")
    raw, text = settings_io.read_raw()
    comments, groups = settings_io.scan(text)
    flat = settings_io.flatten(raw)
    doc, proc, age, running = bot_status()
    if running:
        st.info("The bot is running. Saved changes apply after you restart it (Stop → Start on Bot control).")
    sections = [k for k in SECTION_TITLES if k in raw]
    new_vals = {}
    with st.form("settings"):
        tabs = st.tabs([SECTION_TITLES[k] for k in sections])
        for tab, sec in zip(tabs, sections):
            with tab:
                paths = [p for p in flat if p[0] == sec]
                if sec == "strategy":
                    by_group = {}
                    for p in paths:
                        by_group.setdefault(groups.get(p) or "Other", []).append(p)
                    for g, ps in by_group.items():
                        st.markdown(f"##### {g}")
                        cols = st.columns(3)
                        for i, p in enumerate(ps):
                            with cols[i % 3]:
                                new_vals[p] = _widget(p, flat[p], comments)
                else:
                    if sec == "hedge":
                        st.caption("BUY signal → BUY CALL + SELL PUT · SELL signal → BUY PUT + SELL CALL · "
                                   "pair chosen where Buy LTP − Sell LTP is inside the range.")
                    cols = st.columns(3)
                    for i, p in enumerate(paths):
                        with cols[i % 3]:
                            new_vals[p] = _widget(p, flat[p], comments)
        saved = st.form_submit_button("💾 Save settings", type="primary")
    if saved:
        changes = {p: v for p, v in new_vals.items() if v != flat[p] and not (v == "" and flat[p] is None)}
        if not changes:
            st.info("Nothing changed.")
        else:
            try:
                settings_io.save(changes)
                _push_settings()
                st.success("Saved: " + ", ".join(f"{'.'.join(p)} = {settings_io.fmt_value(v)}" for p, v in changes.items()))
            except Exception as e:  # noqa
                st.error(f"Not saved – invalid settings: {e}")
    c1, c2 = st.columns(2)
    with c1:
        bk = DEFAULT_SETTINGS.with_name("settings.backup.yaml")
        if bk.exists() and st.button("↩ Restore previous settings (undo last save)"):
            shutil.copyfile(bk, DEFAULT_SETTINGS)
            _push_settings()
            for k in [k for k in st.session_state if str(k).startswith("set:")]:
                del st.session_state[k]
            st.rerun()
    with c2:
        st.download_button("⬇ Download settings.yaml", text, file_name="settings.yaml")
    with st.expander("View raw settings.yaml"):
        st.code(text, language="yaml")


# ====================================================================================== page: login
ENV_FIELDS = [
    ("MONGO_URI", True, "MongoDB connection string"), ("MONGO_DB", False, "Database name (default nexus_smc_bot)"),
    ("UPSTOX_API_KEY", False, ""), ("UPSTOX_API_SECRET", True, ""), ("UPSTOX_REDIRECT_URI", False,
                                                                     "Must match the Upstox developer app"),
    ("KITE_API_KEY", False, ""), ("KITE_API_SECRET", True, ""),
    ("TELEGRAM_BOT_TOKEN", True, ""), ("TELEGRAM_CHAT_ID", False, ""),
    ("DASHBOARD_PASSWORD", True, "Password for this dashboard (recommended on a VPS)"),
]


def token_state(broker: str):
    st_ = storage()
    envtok = env(ENV_KEYS[broker])
    doc = st_.find_one("tokens", {"broker": broker}) or {}
    if envtok:
        return "from .env", True, doc
    if doc.get("day") == token_day():
        return f"saved today {str(doc.get('saved_at', ''))[11:16]}", True, doc
    if doc:
        return f"expired (saved {doc.get('day')})", False, doc
    return "not logged in", False, doc


def page_login():
    st.title("🔑 Login & connections")
    st.caption("Broker tokens expire every day (~06:00 IST). Log in each morning before 09:15.")
    s = S()
    # Auto-capture: broker redirects back here with ?code=... (Upstox) or ?request_token=... (Kite)
    qp = st.query_params
    for _b, _p in (("upstox", "code"), ("zerodha", "request_token")):
        _v = qp.get(_p)
        if not _v:
            continue
        try:
            if _b == "upstox":
                from bot.brokers.upstox import UpstoxBroker
                _red = env("UPSTOX_REDIRECT_URI")
                _tok = UpstoxBroker.exchange_code(env("UPSTOX_API_KEY"), env("UPSTOX_API_SECRET"), _red, _v)
            else:
                from bot.brokers.zerodha import ZerodhaBroker
                _tok = ZerodhaBroker.exchange_request_token(env("KITE_API_KEY"), env("KITE_API_SECRET"), _v)
            save_token(storage(), _b, _tok)
            st.success(f"{_b.capitalize()} token captured automatically and saved for today ✅")
        except Exception as e:  # noqa
            st.error(f"{_b.capitalize()} auto-login failed: {e}  "
                     f"(redirect URI used: `{env('UPSTOX_REDIRECT_URI') if _b == 'upstox' else '-'}`)")
        st.query_params.clear()  # codes are single-use; don't retry on rerun
    cols = st.columns(2)
    for col, broker in zip(cols, ["upstox", "zerodha"]):
        with col, st.container(border=True):
            label, ok, _ = token_state(broker)
            st.subheader(("🟢 " if ok else "🔴 ") + broker.capitalize())
            st.write(f"Token: **{label}**" + ("  ·  enabled for trading" if broker in s.brokers.enabled else
                                               "  ·  not enabled") + ("  ·  data source" if broker == s.brokers.data_source else ""))
            if env(ENV_KEYS[broker]):
                st.warning(f"{ENV_KEYS[broker]} is set in .env and overrides the daily login. Clear it below to use the login flow.")
            try:
                if broker == "upstox":
                    from bot.brokers.upstox import UpstoxBroker
                    key, sec, red = env("UPSTOX_API_KEY"), env("UPSTOX_API_SECRET"), env("UPSTOX_REDIRECT_URI")
                    ready = bool(key and sec and red)
                    url = UpstoxBroker.login_url(key, red) if ready else ""
                    param = "code"
                else:
                    from bot.brokers.zerodha import ZerodhaBroker
                    key, sec = env("KITE_API_KEY"), env("KITE_API_SECRET")
                    ready = bool(key and sec)
                    url = ZerodhaBroker.login_url(key) if ready else ""
                    param = "request_token"
            except Exception as e:  # noqa
                ready, url, param = False, "", ""
                st.error(str(e))
            if not ready:
                st.info("Add the API key / secret" + (" / redirect URI" if broker == "upstox" else "") +
                        " in **API keys (.env)** below.")
                continue
            st.markdown("**1.** Open the broker login page and sign in")
            st.link_button(f"Open {broker.capitalize()} login", url, width="stretch")
            st.markdown(f"**2.** After login the browser goes to your redirect URL. Copy that full URL "
                        f"(it contains `{param}=`) and paste it here")
            raw = st.text_input("Redirected URL", key=f"redir_{broker}", placeholder=f"https://...?{param}=...")
            b1, b2 = st.columns(2)
            if b1.button("Save token", key=f"save_{broker}", type="primary", disabled=not raw, width="stretch"):
                try:
                    v = parse_qs(urlparse(raw.strip()).query).get(param, [raw.strip()])[0]
                    if broker == "upstox":
                        tok = UpstoxBroker.exchange_code(key, sec, red, v)
                    else:
                        tok = ZerodhaBroker.exchange_request_token(key, sec, v)
                    save_token(storage(), broker, tok)
                    st.success("Token saved for today ✅")
                except Exception as e:  # noqa
                    st.error(f"Login failed: {e}")
            if b2.button("Test connection", key=f"test_{broker}", width="stretch"):
                test_broker(broker)

    st.subheader("Services")
    c1, c2 = st.columns(2)
    with c1, st.container(border=True):
        st_ = storage()
        if isinstance(st_, LocalStorage):
            st.write("🟡 **Storage: local JSON files** (`data_cache/`)")
            st.caption("Set MONGO_URI below and storage.backend = mongo to use MongoDB. Restart the dashboard after changing it.")
        else:
            st.write(f"🟢 **Storage: MongoDB** (`{env('MONGO_DB', 'nexus_smc_bot')}`)")
        if st.button("Reconnect storage"):
            _storage.clear()
            st.rerun()
    with c2, st.container(border=True):
        tg = bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"))
        st.write(("🟢" if tg else "🔴") + " **Telegram alerts** " + ("configured" if tg else "not configured"))
        if st.button("Send test message", disabled=not tg):
            n = Notifier(True, env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"))
            n._post(f"✅ Nexus SMC dashboard test message {now_ist():%d-%b %H:%M}")
            st.success("Sent – check Telegram.")

    with st.expander("API keys (.env)", expanded=not (ROOT / ".env").exists()):
        if ON_RENDER:
            st.info("Running on Render: set these keys in the Render dashboard under Environment, then redeploy. "
                    "(Keys typed here would be lost on the next restart.)")
            return
        st.caption("Stored in the `.env` file on this machine. Secret fields are hidden; leave a field unchanged to keep it.")
        with st.form("envform"):
            vals = {}
            c = st.columns(2)
            for i, (k, secret, hlp) in enumerate(ENV_FIELDS + [(ENV_KEYS["upstox"], True, "Optional manual token (overrides login)"),
                                                             (ENV_KEYS["zerodha"], True, "Optional manual token (overrides login)")]):
                with c[i % 2]:
                    vals[k] = st.text_input(k, value=env(k), type="password" if secret else "default", help=hlp or None)
            if st.form_submit_button("Save .env", type="primary"):
                envp = ROOT / ".env"
                if not envp.exists():
                    ex = ROOT / ".env.example"
                    shutil.copyfile(ex, envp) if ex.exists() else envp.write_text("", encoding="utf-8")
                changed = [k for k, v in vals.items() if v != env(k)]
                for k in changed:
                    set_key(str(envp), k, vals[k], quote_mode="never")
                load_dotenv(envp, override=True)
                if any(k.startswith("MONGO") for k in changed):
                    _storage.clear()
                st.success(f"Saved {len(changed)} value(s). Restart the bot for it to use them.")


def test_broker(broker: str):
    from bot.live.runner import build_broker
    try:
        with st.spinner("Connecting ..."):
            b = build_broker(broker, storage())
            b.connect()
            msg = "Connected ✅"
            try:
                inst = b.signal_instrument("NIFTY_SPOT")
                px = b.ltp([inst]).get(inst.key)
                msg += f" · Nifty 50 LTP {px:,.2f}" if px else ""
            except Exception:  # noqa
                pass
        st.success(msg)
    except Exception as e:  # noqa
        st.error(f"Connection failed: {e}")


# ====================================================================================== page: history
def page_history():
    st.title("📜 Trade history")
    st_ = storage()
    combos = st_.find("combos")
    rows = []
    for c in combos:
        pnl = c.get("pnl") or {}
        rows.append({
            "id": c.get("id"), "opened": parse_t(c.get("opened_at")), "closed": parse_t(c.get("closed_at")),
            "mode": combo_mode(c), "side": c.get("side"), "trigger": c.get("trigger"), "status": c.get("status"),
            "expiry": c.get("expiry"),
            "buy leg": f"{c.get('buy', {}).get('strike')} {c.get('buy', {}).get('type')}",
            "sell leg": f"{c.get('sell', {}).get('strike')} {c.get('sell', {}).get('type')}",
            "scan diff": c.get("scan_diff"), "Nifty entry": c.get("nifty_entry"), "Nifty SL": c.get("sl"),
            "Nifty target": c.get("target"), "exit reason": c.get("exit_reason", ""),
            **{f"P&L {k}": v for k, v in pnl.items()}, "P&L total": combo_total(c) if pnl else None,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["opened"] = pd.to_datetime(df["opened"])
        df["closed"] = pd.to_datetime(df["closed"])
    tabs = st.tabs(["Option combos", "Strategy signals & events", "Daily summary"])
    with tabs[0]:
        if df.empty:
            st.info("No combos yet. They appear here once the bot trades (paper or live).")
        else:
            f1, f2, f3 = st.columns(3)
            mode = f1.selectbox("Mode", ["all", "paper", "live"])
            status = f2.multiselect("Status", sorted(df["status"].dropna().unique()))
            dmin, dmax = df["opened"].min().date(), df["opened"].max().date()
            rng = f3.date_input("Opened between", (dmin, dmax))
            v = df.copy()
            if mode != "all":
                v = v[v["mode"] == mode]
            if status:
                v = v[v["status"].isin(status)]
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                v = v[(v["opened"].dt.date >= rng[0]) & (v["opened"].dt.date <= rng[1])]
            closed = v[v["status"] == "CLOSED"]
            m = st.columns(5)
            m[0].metric("Combos", len(v))
            m[1].metric("Closed", len(closed))
            tot = closed["P&L total"].sum() if not closed.empty else 0
            m[2].metric("Realized P&L", f"₹{tot:,.0f}")
            wins = (closed["P&L total"] > 0).sum() if not closed.empty else 0
            m[3].metric("Win rate", f"{wins / len(closed) * 100:.1f}%" if len(closed) else "—")
            m[4].metric("Avg per combo", f"₹{closed['P&L total'].mean():,.0f}" if len(closed) else "—")
            if not closed.empty:
                ctrades = [{"exit_time": r["closed"], "pnl": r["P&L total"]} for _, r in closed.iterrows()]
                st.markdown("**Cumulative realized P&L (₹)**")
                st.plotly_chart(charts.equity_chart(ctrades, "pnl", "Cumulative P&L ₹"), width="stretch")
            st.dataframe(v.sort_values("opened", ascending=False), hide_index=True, width="stretch")
            st.download_button("⬇ Download CSV", v.to_csv(index=False), "combos.csv")
            pick = st.selectbox("Combo details", ["—"] + list(v["id"]))
            if pick != "—":
                st.json(next(c for c in combos if c.get("id") == pick))
    with tabs[1]:
        evs = st_.find("events")
        if not evs:
            st.info("No strategy events yet.")
        else:
            e = pd.DataFrame(evs)
            keep = [c for c in ["time", "kind", "direction", "trigger", "price", "sl", "target", "tp1", "rr", "reason",
                                "pnl_points", "mode"] if c in e]
            e = e[keep].sort_values("time", ascending=False)
            kinds = st.multiselect("Event types", sorted(e["kind"].unique()), default=sorted(e["kind"].unique()))
            st.dataframe(e[e["kind"].isin(kinds)], hide_index=True, width="stretch")
            st.download_button("⬇ Download CSV", e.to_csv(index=False), "events.csv")
    with tabs[2]:
        if df.empty or (df["status"] == "CLOSED").sum() == 0:
            st.info("No closed combos yet.")
        else:
            c = df[df["status"] == "CLOSED"].copy()
            c["day"] = c["closed"].dt.date
            daily = c.groupby("day").agg(combos=("id", "count"), pnl=("P&L total", "sum"),
                                         wins=("P&L total", lambda x: (x > 0).sum()))
            st.plotly_chart(charts.daily_pnl_chart(daily), width="stretch")
            st.dataframe(daily.sort_index(ascending=False), width="stretch")


# ====================================================================================== page: backtest
QUICK = ["chart_timeframe", "strat1_IDM", "strat2_CHOCH", "strat3_OB", "strat4_FVG", "strat5_MTF", "strat7_PD_Filter",
         "slMode", "slFixedPreset", "slCustomPoints", "minTargetRr", "enableBreakEven", "tp1RrMultiple",
         "maxTradeDuration", "costPerTradePoints"]


def page_backtest():
    st.title("📈 Backtest – strategy on historical chart")
    s = S()
    src = st.radio("Data", ["Broker history (Upstox/Kite)", "TradingView CSV export", "Demo data (synthetic, no login)"],
                   horizontal=True)
    candles, rows, label = None, None, ""
    if src.startswith("Broker"):
        c1, c2 = st.columns([1, 3])
        days = c1.number_input("Calendar days of history", 5, 730, 90, step=5)
        c2.caption(f"1-minute candles of {s.bot.signal_symbol} from **{s.brokers.data_source}** are downloaded and "
                   f"resampled to the chart timeframe. Needs today's login for {s.brokers.data_source}.")
        if c1.button("Download history"):
            try:
                with st.spinner("Downloading 1-minute candles ... (about 10 s per 28 days)"):
                    from bot.data.candle import session_open_dt
                    from bot.live.runner import build_broker
                    b = build_broker(s.brokers.data_source, storage())
                    b.connect()
                    b.load_instruments()
                    inst = b.signal_instrument(s.bot.signal_symbol)
                    n = now_ist()
                    data = b.candles_1m(inst, session_open_dt(n - timedelta(days=int(days))), n)
                st.session_state["bt_hist"] = (data, f"{s.bot.signal_symbol} {days}d from {s.brokers.data_source}")
            except Exception as e:  # noqa
                st.error(f"Download failed: {e}")
        if "bt_hist" in st.session_state:
            candles, label = st.session_state["bt_hist"]
    elif src.startswith("TradingView"):
        st.caption("TradingView: open NSE:NIFTY with the indicator → ⋯ menu → **Export chart data**. If the file "
                   "contains the indicator's BUY/SELL columns, a bar-by-bar parity check is shown.")
        up = st.file_uploader("CSV file", type=["csv"])
        if up:
            from backtest.runner import load_csv
            tmp = ROOT / "data_cache" / "uploaded_tv.csv"
            tmp.parent.mkdir(exist_ok=True)
            tmp.write_bytes(up.getvalue())
            candles, rows = load_csv(tmp)
            label = up.name
    else:
        from tests.fakes import synthetic_1m
        c1, c2 = st.columns(2)
        dd = c1.number_input("Days", 10, 250, 60)
        seed = c2.number_input("Random seed", 1, 9999, 7)
        st.caption("Random-walk data only to try the dashboard – results mean nothing for the real market.")
        candles, label = synthetic_1m(int(dd), int(seed)), f"synthetic {dd}d seed {seed}"

    with st.expander("Quick overrides for this backtest (saved settings are not changed)"):
        base = s.strategy
        ov = {}
        cols = st.columns(3)
        for i, k in enumerate(QUICK):
            with cols[i % 3]:
                v = getattr(base, k)
                p = ("bot", "chart_timeframe") if k == "chart_timeframe" else ("strategy", k)
                ov[k] = _widget(p, v, {}, "bt:") if k != "chart_timeframe" else \
                    st.selectbox("Chart timeframe (min)", CHOICES["bot.chart_timeframe"],
                                 index=CHOICES["bot.chart_timeframe"].index(v) if v in CHOICES["bot.chart_timeframe"] else 2,
                                 key="bt_tf")
        st.markdown("**Rupee estimate**")
        r1, r2 = st.columns(2)
        lots = r1.number_input("Lots", 1, 100, int(sum((s.brokers.get("lots") or {"x": 1}).values())))
        lot_size = r2.number_input("Lot size", 1, 500, 65)
        st.caption("BUY CALL + SELL PUT moves about 1:1 with Nifty (synthetic future), so ₹ ≈ Nifty points × lots × lot size. "
                   "Premium difference, slippage and charges are not included.")

    if candles and st.button("▶ Run backtest", type="primary"):
        from backtest.runner import run_backtest
        cfg = dataclasses.replace(s.strategy, **ov)
        with st.spinner(f"Running strategy on {len(candles):,} candles ..."):
            res = run_backtest(candles, cfg, rows, ROOT / "logs" / "backtest")
        st.session_state["bt_res"] = (res, cfg.chart_timeframe, label, lots * lot_size)

    if "bt_res" not in st.session_state:
        return
    res, tf, label, mult = st.session_state["bt_res"]
    trades = charts.trade_dicts(res["trades"])
    for t in trades:
        t["rupees"] = round((t.get("pnl_points") or 0) * mult, 0) if t.get("exit_time") else None
    stx = res["stats"]
    st.subheader(f"Results – {label}")
    closed = [t for t in trades if t.get("exit_time")]
    rs = [t.get("net_r") or 0 for t in closed]
    gross_w = sum(r for r in rs if r > 0)
    gross_l = -sum(r for r in rs if r < 0)
    m = st.columns(7)
    m[0].metric("Bars", f"{res['bars']:,}", f"{tf}m candles", delta_color="off", delta_arrow="off")
    m[1].metric("Trades", stx["trades"])
    m[2].metric("Win rate", f"{stx['win_rate']}%", f"{stx['wins']}W / {stx['losses']}L / {stx['breakeven']}BE", delta_color="off", delta_arrow="off")
    m[3].metric("Net R", f"{stx['net_r']:+.2f}")
    m[4].metric("Profit factor", f"{gross_w / gross_l:.2f}" if gross_l else "—")
    m[5].metric("Max drawdown", f"{charts.max_drawdown(rs):.2f} R")
    m[6].metric("≈ P&L", f"₹{sum(t['rupees'] or 0 for t in closed):,.0f}")
    if "parity" in res:
        p = res["parity"]
        (st.success if p["mismatched_bars"] == 0 else st.warning)(
            f"TradingView parity: TV buys {p['tv_buys']} / sells {p['tv_sells']} · Python buys {p['py_buys']} / "
            f"sells {p['py_sells']} · mismatched bars **{p['mismatched_bars']}**")
        if p["first_mismatches"]:
            st.dataframe(pd.DataFrame(p["first_mismatches"], columns=["bar time", "difference"]), hide_index=True)

    # ------------------------------------------------------------- chart
    bars = [{"t": c.time.isoformat(), "o": c.open, "h": c.high, "l": c.low, "c": c.close,
             "sh": d.get("protectedStrongHigh"), "sl": d.get("protectedStrongLow"), "eq": d.get("equilibrium")}
            for c, d in zip(res["feed"], res["diag"])]
    df = charts.bars_frame(bars)
    if df.empty:
        return
    days = sorted(df["t"].dt.date.unique())
    c1, c2, c3, c4 = st.columns([2, 3, 1, 1])
    view = c1.radio("View", ["Date range", "One trade"], horizontal=True)
    show_lv = c3.toggle("Structure levels", True)
    show_sl = c4.toggle("SL / target", True)
    if view == "One trade" and trades:
        names = [f"#{i + 1} {t['entry_time'][:16].replace('T', ' ')} {'BUY' if t['direction'] == 1 else 'SELL'} "
                 f"{t['trigger']} → {t.get('reason') or 'open'} {(t.get('pnl_points') or 0):+.1f} pts"
                 for i, t in enumerate(trades)]
        idx = c2.selectbox("Trade", range(len(trades)), format_func=lambda i: names[i], index=len(trades) - 1)
        t = trades[idx]
        a, b = charts.window_for(t, tf)
        sub = df[(df["t"] >= a - timedelta(days=4)) & (df["t"] <= b + timedelta(days=4))]
        fig = charts.price_chart(sub, trades, tf, show_lv, show_sl, height=620, xrange=(a, b))
        cols = st.columns(6)
        cols[0].metric("Direction", "BUY" if t["direction"] == 1 else "SELL", t["trigger"], delta_color="off", delta_arrow="off")
        cols[1].metric("Entry", f"{t['entry']:,.2f}")
        cols[2].metric("SL", f"{t['sl']:,.2f}", f"{abs(t['entry'] - t['sl']):.1f} pts", delta_color="off", delta_arrow="off")
        cols[3].metric("Target", f"{t['target']:,.2f}", f"R:R {t['rr']:.2f}", delta_color="off", delta_arrow="off")
        cols[4].metric("Exit", f"{t['exit']:,.2f}" if t.get("exit") else "open", t.get("reason") or None, delta_color="off", delta_arrow="off")
        cols[5].metric("Result", f"{(t.get('pnl_points') or 0):+.1f} pts", f"₹{t['rupees'] or 0:,.0f}")
    else:
        default = (days[max(0, len(days) - 10)], days[-1])
        rng = c2.slider("Dates shown", min_value=days[0], max_value=days[-1], value=default, format="DD MMM YY") \
            if len(days) > 1 else (days[0], days[0])
        sub = df[(df["t"].dt.date >= rng[0]) & (df["t"].dt.date <= rng[1])]
        tsub = [t for t in trades if rng[0] <= pd.Timestamp(t["entry_time"]).date() <= rng[1]
                or (t.get("exit_time") and rng[0] <= pd.Timestamp(t["exit_time"]).date() <= rng[1])]
        fig = charts.price_chart(sub, tsub, tf, show_lv, show_sl, height=620)
    st.plotly_chart(fig, width="stretch")
    st.caption("▲ BUY entry · ▼ SELL entry · ✕ exit (green = profit, red = loss) · dashed red/green = SL / target "
               "while the trade was open · brown/blue lines = protected strong high / low. Scroll to zoom, drag to pan.")

    # ------------------------------------------------------------- equity + tables
    e1, e2 = st.columns(2)
    with e1:
        st.markdown("**Equity curve (net R)**")
        st.plotly_chart(charts.equity_chart(trades), width="stretch")
    with e2:
        st.markdown("**Equity curve (≈ ₹)**")
        st.plotly_chart(charts.equity_chart(trades, "rupees", "≈ ₹"), width="stretch")
    if trades:
        tdf = pd.DataFrame(trades)
        tdf["side"] = tdf["direction"].map({1: "BUY", -1: "SELL"})
        for col in ("entry_time", "exit_time"):
            tdf[col] = tdf[col].map(lambda x: str(x)[:16].replace("T", " ") if x else None)
        cols = ["side", "trigger", "entry_time", "entry", "sl", "target", "tp1", "rr", "exit_time", "exit", "reason",
                "pnl_points", "net_r", "rupees"]
        t1, t2, t3 = st.tabs(["All trades", "By trigger / exit reason", "By month"])
        with t1:
            st.dataframe(tdf[cols].round(2), hide_index=True, width="stretch")
            st.download_button("⬇ Download trades CSV", tdf[cols].to_csv(index=False), "backtest_trades.csv")
        cl = tdf[tdf["exit_time"].notna()].copy()
        with t2:
            if not cl.empty:
                agg = dict(trades=("net_r", "count"), win_rate=("net_r", lambda x: round((x > 0).mean() * 100, 1)),
                           net_r=("net_r", "sum"), points=("pnl_points", "sum"), rupees=("rupees", "sum"))
                a, b = st.columns(2)
                a.dataframe(cl.groupby(["side", "trigger"]).agg(**agg).round(2), width="stretch")
                b.dataframe(cl.groupby("reason").agg(**agg).round(2), width="stretch")
        with t3:
            if not cl.empty:
                cl["month"] = pd.to_datetime(cl["exit_time"].astype(str).str[:7])
                mm = cl.groupby(cl["month"].dt.strftime("%Y-%m")).agg(
                    trades=("net_r", "count"), win_rate=("net_r", lambda x: round((x > 0).mean() * 100, 1)),
                    net_r=("net_r", "sum"), points=("pnl_points", "sum"), rupees=("rupees", "sum")).round(2)
                st.dataframe(mm, width="stretch")


# ====================================================================================== page: logs
def page_logs():
    st.title("📄 Logs")
    files = {"Bot log (logs/bot.log)": ROOT / "logs" / "bot.log",
             "Bot console (started from dashboard)": process.CONSOLE_LOG}
    c1, c2, c3 = st.columns([2, 1, 2])
    which = c1.selectbox("File", list(files))
    n = c2.number_input("Lines", 50, 5000, 300, step=50)
    flt = c3.text_input("Filter (text)", placeholder="e.g. ALERT, ERROR, Combo")
    txt = process.tail(files[which], int(n * (5 if flt else 1)))
    if flt:
        txt = "\n".join(l for l in txt.splitlines() if flt.lower() in l.lower())[-200_000:]
    st.button("↻ Refresh")
    st.code(txt or "(empty)", language="log")


# ====================================================================================== main
def main():
    if not check_password():
        return
    pages = {"🤖 Bot control": page_control, "⚙️ Settings": page_settings, "🔑 Login & connections": page_login,
             "📜 History": page_history, "📈 Backtest": page_backtest, "📄 Logs": page_logs}
    with st.sidebar:
        st.markdown("## 📈 Nexus SMC Bot")
        _oauth = bool(st.query_params.get("code") or st.query_params.get("request_token"))
        choice = st.radio("Page", list(pages), index=2 if _oauth else 0, label_visibility="collapsed")
        st.divider()
        try:
            doc, proc, age, running = bot_status()
            st.write(("🟢 Running" if running else "🔴 Stopped") +
                     (f" · {(doc.get('mode') or '').upper()}" if running else ""))
            for b in ("upstox", "zerodha"):
                lbl, ok, _ = token_state(b)
                st.caption(f"{'🟢' if ok else '🔴'} {b}: {lbl}")
            st_ = storage()
            st.caption("🟢 MongoDB" if not isinstance(st_, LocalStorage) else "🟡 Local JSON storage")
        except Exception as e:  # noqa
            st.caption(f"status unavailable: {e}")
        if not env("DASHBOARD_PASSWORD"):
            st.caption("⚠ No DASHBOARD_PASSWORD set – anyone who can reach this port can control the bot.")
    pages[choice]()


main()
