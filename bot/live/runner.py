"""Live / paper trading loop.

Flow:
 1. connect brokers, load instruments
 2. warm-up: replay history through the engine (no orders) -> same state as TradingView
 3. sync stored open combos with the engine position
 4. loop: at every chart-candle close -> engine -> ENTRY / EXIT / SL_MOVE / TP1 events -> options combos
          every ltp_poll_sec   -> tick SL / target monitoring, rupee SL/target, expiry-day exit, kill switch
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from ..brokers.base import Broker, BrokerError
from ..brokers.paper import PaperBroker
from ..config import ROOT, Settings, env
from ..data.candle import (bucket_bounds, closed_only, now_ist, resample, session_close_dt,
                           session_open_dt)
from ..hedge.executor import ComboExecutor
from ..hedge.expiry import select_expiry
from ..hedge.scanner import find_pair, strike_window
from ..services.notifier import Notifier
from ..services.storage import Storage
from ..services.tokens import get_token
from ..strategy.engine import Event, NexusEngine

log = logging.getLogger("runner")
KILL_FILE = ROOT / "KILL"
STOP_FILE = ROOT / "STOP"      # graceful stop (dashboard): leaves positions as they are
CHART_BARS = 400


def build_broker(name: str, storage: Storage) -> Broker:
    if name == "upstox":
        from ..brokers.upstox import UpstoxBroker
        return UpstoxBroker(get_token(storage, "upstox"))
    if name == "zerodha":
        from ..brokers.zerodha import ZerodhaBroker
        return ZerodhaBroker(env("KITE_API_KEY"), get_token(storage, "zerodha"))
    raise ValueError(f"unknown broker {name}")


class LiveBot:
    def __init__(self, settings: Settings, storage: Storage, notifier: Notifier, mode: str):
        self.S = settings
        self.storage = storage
        self.notify = notifier
        self.mode = mode
        self.tf = int(settings.bot.chart_timeframe)
        self.engine: Optional[NexusEngine] = None
        self.data: Optional[Broker] = None
        self.exec_brokers: Dict[str, object] = {}
        self.executor: Optional[ComboExecutor] = None
        self.signal_inst = None
        self.last_bar_time: Optional[datetime] = None
        self.prepared_day = None
        self.running = True
        self.last_rs_check = 0.0
        self.summary_sent_day = None
        self.state = "STARTING"
        self.state_msg = ""
        self.last_ltp: Optional[float] = None
        self.last_ltp_at: Optional[datetime] = None      # when the last LTP tick was received
        self.last_candle_at: Optional[datetime] = None   # start time of the newest 1m candle received
        self.data_err = ""
        self.last_hb = 0.0
        self.started_at = now_ist().isoformat()
        self.recent = deque(maxlen=CHART_BARS)   # recent bars + levels for the dashboard live chart

    # ------------------------------------------------------------------ setup
    def connect(self):
        names = list(self.S.brokers.enabled or [])
        ds = self.S.brokers.data_source
        built: Dict[str, Broker] = {}
        needed = set(names + [ds]) if self.mode == "live" else {ds}   # paper only needs the data broker
        for n in needed:
            b = build_broker(n, self.storage)
            b.connect()
            b.load_instruments()
            built[n] = b
        self.data = built[ds]
        if self.mode == "live":
            self.exec_brokers = {n: built[n] for n in names}
        else:
            self.exec_brokers = {f"paper-{n}": PaperBroker(self.data, n) for n in names}
        self.executor = ComboExecutor(self.exec_brokers, self.S.brokers, self.storage, self.notify)
        self.signal_inst = self.data.signal_instrument(self.S.bot.signal_symbol)
        log.info("TICKER: %s (%s) | data source: %s | chart TF %sm", self.S.bot.signal_symbol,
                 self.signal_inst.symbol, ds, self.tf)
        log.info("Signal instrument: %s | execution: %s | mode: %s", self.signal_inst.symbol,
                 list(self.exec_brokers), self.mode)

    def warmup(self):
        self.engine = NexusEngine(self.S.strategy)
        bars_needed = int(self.S.bot.warmup_bars)
        trading_days = bars_needed * self.tf / 375 + 2
        cal_days = int(trading_days * 7 / 5) + 7
        n = now_ist()
        start = session_open_dt(n - timedelta(days=cal_days))
        log.info("Warm-up: downloading ~%d days of 1m candles ...", cal_days)
        m1 = self.data.candles_1m(self.signal_inst, start, n)
        bars = closed_only(resample(m1, self.tf), self.tf, n)[-bars_needed:]
        for b in bars:
            self.engine.on_bar(b)  # events ignored: history
            self._remember(b)
        self.last_bar_time = bars[-1].time if bars else None
        st = self.engine.stats()
        pos = self.engine.position()
        log.info("Warm-up done: %d bars, last %s | stats %s | position %s", len(bars), self.last_bar_time, st, pos)
        self.notify.send(f"Nexus SMC bot started ({self.mode})\nWarm-up {len(bars)} bars, last bar {self.last_bar_time}\n"
                         f"Trend: {'BULL' if self.engine.trendBias == 1 else 'BEAR' if self.engine.trendBias == -1 else '-'}"
                         f" | Strategy position: {pos['dir']}\nBacktest stats: {st}")
        self.save_chart()

    def sync_positions(self):
        open_combos = self.open_combos()
        d = self.engine.activeTradeDir
        for c in open_combos:
            if d == 0 or c["direction"] != d:
                self.notify.send(f"Restart sync: strategy is flat/opposite -> closing combo {c['id']}")
                self._close(c, "RESTART_SYNC")
        if d != 0 and not self.open_combos() and self.S.safety.get("enter_on_restart", False):
            p = self.engine.position()
            ev = Event("ENTRY", self.engine.bar_index, str(self.last_bar_time), d, p["trigger"], p["entry"],
                       p["sl"], p["target"], p["tp1"], p["rr"])
            self.handle_entry(ev, late=True)

    # ------------------------------------------------------------------ helpers
    def open_combos(self) -> List[dict]:
        return [c for c in self.storage.find("combos") if c.get("status") in ("OPEN", "CLOSE_FAILED")]

    def today_combos(self) -> List[dict]:
        day = now_ist().date().isoformat()
        return [c for c in self.storage.find("combos") if str(c.get("opened_at", "")).startswith(day)]

    def kill_active(self) -> bool:
        return bool(self.S.safety.get("kill_switch")) or KILL_FILE.exists()

    def _in_window(self, n: datetime) -> bool:
        a, b = self.S.safety.get("entry_window_start") or "", self.S.safety.get("entry_window_end") or ""
        if not a or not b:
            return True
        hm = n.strftime("%H:%M")
        return a <= hm <= b

    # ------------------------------------------------------------------ events
    def handle_events(self, events: List[Event]):
        for ev in events:
            self.storage.insert("events", {**ev.to_dict(), "mode": self.mode, "logged_at": now_ist().isoformat()})
            if ev.kind == "ENTRY":
                self.handle_entry(ev)
            elif ev.kind == "EXIT":
                self.notify.send(f"{'🟢' if ev.direction == 1 else '🔴'} Strategy EXIT ({ev.reason}) at Nifty {ev.price:.2f} "
                                 f"| pts {ev.pnl_points:+.2f}")
                for c in self.open_combos():
                    self._close(c, ev.reason)
            elif ev.kind == "SL_MOVE":
                self.notify.send(f"🛡 SL moved to break-even: {ev.sl:.2f}")
                for c in self.open_combos():
                    c["sl"] = ev.sl
                    self.storage.upsert("combos", {"id": c["id"]}, {"sl": ev.sl})
            elif ev.kind == "TP1":
                self.notify.send(f"🎯 TP1 reached at {ev.price:.2f} (SL -> entry from next candle)")

    def handle_entry(self, ev: Event, late: bool = False):
        side = "BUY" if ev.direction == 1 else "SELL"
        icon = "🟢" if ev.direction == 1 else "🔴"
        msg = (f"{icon} Nexus SMC {side} Signal | Price: {ev.price:.2f} | Trigger: {ev.trigger} | "
               f"SL: {ev.sl:.2f} | Target: {ev.target:.2f} | R:R {ev.rr:.2f}")
        self.storage.insert("signals", {**ev.to_dict(), "mode": self.mode})
        self.notify.send(msg + (" (late entry after restart)" if late else ""))
        n = now_ist()
        sf = self.S.safety
        if self.kill_active():
            return self.notify.send("⛔ Kill switch active - signal not traded")
        if not self._in_window(n):
            return self.notify.send("⏸ Outside entry time window - signal not traded")
        if int(sf.get("max_trades_per_day") or 0) and len(self.today_combos()) >= int(sf.max_trades_per_day):
            return self.notify.send("⏸ Max trades per day reached - signal not traded")
        if float(sf.get("max_daily_loss_rs") or 0):
            realized = sum(sum((c.get("pnl") or {}).values()) for c in self.today_combos() if c.get("status") == "CLOSED")
            if realized <= -abs(float(sf.max_daily_loss_rs)):
                return self.notify.send("⏸ Max daily loss reached - signal not traded")
        # opposite combos (only possible when duplicate filter is off) are closed first
        for c in self.open_combos():
            if c["direction"] != ev.direction:
                self._close(c, "REVERSE")
        if self.open_combos() and not self.S.strategy.allowMultiTriggerEntries:
            return self.notify.send("Combo already open - not adding")
        self.open_hedge(ev)

    def open_hedge(self, ev: Event):
        H = self.S.hedge
        expiry = select_expiry(self.data.option_expiries(), H.expiry_mode, now_ist().date(),
                               H.get("expiry_date", ""), bool(H.get("use_next_expiry_on_expiry_day", True)))
        if not expiry:
            return self.notify.send("❌ No valid expiry found - trade skipped")
        deadline = time.time() + float(H.get("max_rescan_sec", 60))
        pair = None
        while True:
            ref = self.data.ltp([self.signal_inst]).get(self.signal_inst.key) or ev.price
            _, strikes = strike_window(ref, int(H.get("strike_step", 50)), int(H.get("strike_scan_width", 10)))
            chain = self.data.option_chain(expiry, strikes)
            pair, matches = find_pair(ev.direction, chain, ref, H)
            if pair or time.time() >= deadline or self.stop_requested():
                break
            log.info("No pair in range %s-%s yet, rescanning", H.range_min, H.range_max)
            time.sleep(float(H.get("rescan_interval_sec", 5)))
        if not pair:
            return self.notify.send(f"⚠ No option pair with premium difference {H.range_min}-{H.range_max} "
                                    f"found for {expiry} - trade skipped")
        self.notify.send(f"🔎 Pair selected ({pair.candidates} matches), expiry {expiry}:\n{pair.describe()}")
        combo = self.executor.open_combo(pair, expiry, {**ev.to_dict(), "mode": self.mode})
        lines = [f"{'✅' if combo['status'] == 'OPEN' else '❌'} Combo {combo['id']} {combo['status']}"]
        for n, leg in combo["legs"].items():
            lines.append(f"{n}: {leg['status']} | BUY {leg['buy'].get('symbol')} x{leg['buy'].get('qty', 0)} @ "
                         f"{leg['buy'].get('entry', 0)} | SELL {leg['sell'].get('symbol')} x{leg['sell'].get('qty', 0)} @ "
                         f"{leg['sell'].get('entry', 0)} {leg.get('msg', '')}")
        self.notify.send("\n".join(lines))

    def _close(self, combo: dict, reason: str):
        c = self.executor.close_combo(combo, reason)
        pnl = c.get("pnl", {})
        self.notify.send(f"{'✅' if c['status'] == 'CLOSED' else '🚨'} Combo {c['id']} {c['status']} ({reason}) "
                         f"P&L: " + ", ".join(f"{k}: ₹{v:,.0f}" for k, v in pnl.items()))
        if c["status"] == "CLOSE_FAILED":
            self.notify.send("🚨 EXIT INCOMPLETE - check broker positions manually NOW")

    # ------------------------------------------------------------------ candle processing
    def process_new_bars(self):
        n = now_ist()
        delay = float(self.S.bot.get("candle_close_delay_sec", 3))
        try:
            m1 = self.data.candles_1m_today(self.signal_inst)
        except BrokerError as e:
            log.warning("candle fetch failed: %s", e)
            self.data_err = f"candle fetch failed: {e}"
            return
        self.data_err = ""
        if m1:
            self.last_candle_at = max(c.time for c in m1)
        m1_times = {c.time for c in m1}
        m1 = [c for c in m1 if c.time + timedelta(minutes=1) <= n]
        bars = resample(m1, self.tf)
        for b in bars:
            if self.last_bar_time and b.time <= self.last_bar_time:
                continue
            _, end = bucket_bounds(b.time, self.tf)
            if end + timedelta(seconds=delay) > n:
                break
            last_minute = end - timedelta(minutes=1)
            if last_minute not in m1_times and n < end + timedelta(seconds=60):
                break  # broker has not published the last 1m candle of this bar yet
            events = self.engine.on_bar(b)
            self.last_bar_time = b.time
            self._remember(b)
            self.save_chart()
            log.info("Bar %s O%.2f H%.2f L%.2f C%.2f | trend %d | pos %d | events %s", b.time.strftime("%d-%b %H:%M"),
                     b.open, b.high, b.low, b.close, self.engine.trendBias, self.engine.activeTradeDir,
                     [e.kind for e in events])
            self.handle_events(events)

    def catch_up(self):
        """Feed any bars missed while the process was idle (e.g. previous session)."""
        if not self.last_bar_time:
            return
        n = now_ist()
        m1 = self.data.candles_1m(self.signal_inst, self.last_bar_time, n)
        for b in closed_only(resample(m1, self.tf), self.tf, n):
            if b.time > self.last_bar_time and b.time.date() < n.date():
                self.handle_events(self.engine.on_bar(b))
                self.last_bar_time = b.time
                self._remember(b)
        self.save_chart()

    # ------------------------------------------------------------------ monitoring
    def monitor(self):
        combos = self.open_combos()
        if not combos:
            return
        n = now_ist()
        H = self.S.hedge
        # retry incomplete exits every 30 s
        if time.time() - getattr(self, "_last_retry", 0) > 30:
            self._last_retry = time.time()
            for c in combos:
                if c.get("status") == "CLOSE_FAILED":
                    self._close(c, c.get("exit_reason") or "RETRY")
            combos = self.open_combos()
        # expiry-day EOD exit
        if H.get("expiry_day_exit", True):
            hm = n.strftime("%H:%M")
            for c in combos:
                if c.get("expiry") == n.date().isoformat() and hm >= H.get("expiry_exit_time", "15:15"):
                    self._close(c, "EXPIRY_EOD")
            combos = self.open_combos()
        # tick SL / target on Nifty level
        if self.S.bot.get("exit_monitoring", "tick") == "tick" and combos and self.engine.activeTradeDir != 0:
            ltp = self.data.ltp([self.signal_inst]).get(self.signal_inst.key)
            e = self.engine
            if ltp:
                for c in combos:
                    d = c["direction"]
                    if d != e.activeTradeDir:
                        continue
                    if (d == 1 and ltp <= e.slPrice) or (d == -1 and ltp >= e.slPrice):
                        self.notify.send(f"Nifty {ltp:.2f} hit SL {e.slPrice:.2f}")
                        self._close(c, "SL_TICK")
                    elif (d == 1 and ltp >= e.targetPrice) or (d == -1 and ltp <= e.targetPrice):
                        self.notify.send(f"Nifty {ltp:.2f} hit Target {e.targetPrice:.2f}")
                        self._close(c, "TARGET_TICK")
        # rupee SL / target (optional)
        sl_rs, tg_rs = float(H.get("combo_sl_rs") or 0), float(H.get("combo_target_rs") or 0)
        if (sl_rs or tg_rs) and time.time() - self.last_rs_check > 5:
            self.last_rs_check = time.time()
            for c in self.open_combos():
                mtm = self.executor.combo_mtm(c, self.data)
                if not mtm:
                    continue
                worst = min(mtm.values())
                if sl_rs and worst <= -abs(sl_rs):
                    self._close(c, "COMBO_SL_RS")
                elif tg_rs and worst >= abs(tg_rs):
                    self._close(c, "COMBO_TARGET_RS")

    def daily_prepare(self):
        n = now_ist()
        log.info("Daily preparation for %s", n.date())
        while True:
            try:
                self.connect()
                break
            except Exception as e:  # noqa
                self.state, self.state_msg = "WAITING_LOGIN", str(e)
                self.heartbeat(force=True)
                self.notify.send(f"🔑 Broker login needed / failed: {e}\nRun: python main.py login <broker>. Retrying in 60s")
                for _ in range(60):
                    if self.stop_requested():
                        return False
                    time.sleep(1)
        if self.engine is None:
            self.warmup()
            self.sync_positions()
        else:
            self.catch_up()
        self.prepared_day = n.date()
        self.state, self.state_msg = "RUNNING", ""
        return True

    def send_summary(self):
        combos = self.today_combos()
        pnl = sum(sum((c.get("pnl") or {}).values()) for c in combos if c.get("status") == "CLOSED")
        self.notify.send(f"📊 Day summary {now_ist().date()}: combos {len(combos)}, realized P&L ₹{pnl:,.0f}\n"
                         f"Strategy stats: {self.engine.stats()}\nOpen position: {self.engine.position()}")

    # ------------------------------------------------------------------ main loop
    def run(self):
        STOP_FILE.unlink(missing_ok=True)
        self.heartbeat(force=True)
        if not self.daily_prepare():
            return self._stopped("stopped while waiting for login")
        poll = float(self.S.bot.get("ltp_poll_sec", 1.0))
        while self.running:
            try:
                n = now_ist()
                if self.stop_requested():
                    self._stopped("stop requested from dashboard (positions left as they are)")
                    break
                if n.date() != self.prepared_day and n.hour >= 8 and n.weekday() < 5:
                    if not self.daily_prepare():
                        self._stopped("stopped while waiting for login")
                        break
                if self.kill_active():
                    for c in self.open_combos():
                        self._close(c, "KILL_SWITCH")
                    self.notify.send("⛔ Kill switch: all combos closed, bot stopped")
                    self._stopped("kill switch", state="KILLED")
                    break
                market = n.weekday() < 5 and session_open_dt(n) <= n <= session_close_dt(n) + timedelta(minutes=2)
                self.state = "RUNNING" if market else "MARKET_CLOSED"
                self.heartbeat()
                if market:
                    self.process_new_bars()
                    self.monitor()
                elif n > session_close_dt(n) + timedelta(minutes=5) and self.summary_sent_day != n.date() and n.weekday() < 5:
                    self.send_summary()
                    self.summary_sent_day = n.date()
                time.sleep(poll if market else 5)
            except KeyboardInterrupt:
                log.info("Stopped by user")
                self._stopped("stopped by user (Ctrl+C)")
                break
            except Exception as e:  # noqa – keep the bot alive
                log.exception("loop error")
                self.notify.send(f"⚠ Bot error: {e}")
                time.sleep(5)

    # ------------------------------------------------------------------ dashboard support
    def stop_requested(self) -> bool:
        return STOP_FILE.exists()

    def _stopped(self, msg: str, state: str = "STOPPED"):
        STOP_FILE.unlink(missing_ok=True)
        log.info("Bot stopped: %s", msg)
        self.state, self.state_msg = state, msg
        self.heartbeat(force=True)

    def _remember(self, b):
        d = self.engine.diag
        self.recent.append({"t": b.time.isoformat(), "o": b.open, "h": b.high, "l": b.low, "c": b.close,
                            "trend": d.get("trendBias"), "sh": d.get("protectedStrongHigh"),
                            "sl": d.get("protectedStrongLow"), "eq": d.get("equilibrium")})

    def save_chart(self):
        try:
            self.storage.upsert("chart", {"id": "live"}, {"id": "live", "tf": self.tf, "bars": list(self.recent),
                                                         "updated_at": now_ist().isoformat()})
        except Exception as e:  # noqa
            log.warning("chart snapshot failed: %s", e)

    def heartbeat(self, force: bool = False):
        """Status document read by the dashboard (written every ~5 s)."""
        if not force and time.time() - self.last_hb < 5:
            return
        self.last_hb = time.time()
        try:
            mtm = {}
            if self.data is not None and self.signal_inst is not None and self.state == "RUNNING":
                try:
                    px = self.data.ltp([self.signal_inst]).get(self.signal_inst.key)
                    if px:
                        self.last_ltp, self.last_ltp_at = px, now_ist()
                        self.data_err = ""
                    for c in self.open_combos():
                        mtm[c["id"]] = self.executor.combo_mtm(c, self.data)
                except Exception as e:  # noqa
                    self.data_err = f"ltp failed: {e}"
                    log.debug("heartbeat ltp failed: %s", e)
            e = self.engine
            self.storage.upsert("status", {"id": "bot"}, {
                "id": "bot", "pid": os.getpid(), "mode": self.mode, "state": self.state, "msg": self.state_msg,
                "started_at": self.started_at, "updated_at": now_ist().isoformat(),
                "signal_symbol": self.S.bot.signal_symbol, "tf": self.tf,
                "brokers": list(self.exec_brokers), "data_source": self.S.brokers.data_source,
                "ltp": self.last_ltp, "ltp_at": self.last_ltp_at.isoformat() if self.last_ltp_at else None,
                "last_candle_at": self.last_candle_at.isoformat() if self.last_candle_at else None,
                "data_err": self.data_err, "last_bar": self.last_bar_time.isoformat() if self.last_bar_time else None,
                "trend": e.trendBias if e else None, "htf_bias": (e.diag.get("htfBias") if e else None),
                "position": e.position() if e else None, "stats": e.stats() if e else None,
                "mtm": mtm,
            })
        except Exception as ex:  # noqa
            log.warning("heartbeat failed: %s", ex)
