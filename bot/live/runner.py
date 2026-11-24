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
import re
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
from ..hedge.expiry import choose_expiry, save_expiry_list
from ..hedge.scanner import find_pair, range_text, strike_window
from ..services import messages as M
from ..services.notifier import Notifier
from ..services.storage import Storage
from ..services.tokens import get_token
from ..strategy.engine import Event, NexusEngine

log = logging.getLogger("runner")
KILL_FILE = ROOT / "KILL"
STOP_FILE = ROOT / "STOP"      # graceful stop (dashboard): leaves positions as they are
CHART_BARS = 400


def trade_id(ev) -> str:
    """Stable, human-readable id of one punched trade, e.g. B2311-1015-OB (Buy, 23-Nov, signal bar 10:15, trigger OB).
    Derived from the signal itself, so it is the same in Telegram, the dashboard and every stored record."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", str(ev.time))
    stamp = f"{m.group(3)}{m.group(2)}-{m.group(4)}{m.group(5)}" if m else str(ev.bar_index)
    trig = re.sub(r"[^A-Za-z+]", "", str(ev.trigger or "")).upper()
    return f"{'B' if ev.direction == 1 else 'S'}{stamp}" + (f"-{trig}" if trig else "")


def combo_status_line(c: dict) -> str:
    """One Telegram line for a stored combo: id, side, state and whether TP1 was reached."""
    state = {"OPEN": "OPEN", "CLOSED": "CLOSED", "CLOSE_FAILED": "CLOSE FAILED",
             "FAILED": "NOT OPENED"}.get(c.get("status"), c.get("status"))
    pnl = c.get("pnl") or {}
    if c.get("status") == "CLOSED":
        tail = f" | exit {c.get('exit_reason')} | P&L Rs{sum(pnl.values()):,.0f}"
    else:
        tail = f" | {'TP1 hit' if c.get('tp1_hit') else 'TP1 pending'}"
        if c.get("sl") is not None and c.get("target") is not None:
            tail += f" | SL {c['sl']:.2f} | Tgt {c['target']:.2f}"
    return f"[{c.get('tid') or c.get('id')}] {c.get('side', '')} {state}{tail} | expiry {c.get('expiry', '-')}"


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
        try:   # real exchange expiries, so the dashboard can let the client pick an exact one
            save_expiry_list(self.storage, self.S.bot.signal_symbol, self.data.option_expiries())
        except Exception:  # noqa
            log.exception("could not store expiry list")
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
        ver = self.S.strategy.strategy_version
        log.info("Warm-up done: %d bars, last %s | logic %s | stats %s | position %s", len(bars), self.last_bar_time,
                 ver, st, pos)
        self.notify.send(f"Nexus SMC bot started ({self.mode}) · logic {ver}\nWarm-up {len(bars)} bars, last bar {self.last_bar_time}\n"
                         f"Trend: {'BULL' if self.engine.trendBias == 1 else 'BEAR' if self.engine.trendBias == -1 else '-'}"
                         f" | Strategy position: {pos['dir']}\nBacktest stats: {st}"
                         + self._open_lines("\n\nOpen positions carried over:"))
        self.save_chart()

    def sync_positions(self):
        open_combos = self.open_combos()
        d = self.engine.activeTradeDir
        for c in open_combos:
            if d == 0 or c["direction"] != d:
                self.notify.send(f"Restart sync: strategy is flat/opposite -> closing [{c.get('tid') or c['id']}] combo {c['id']}")
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

    def _open_lines(self, head: str) -> str:
        oc = self.open_combos()
        return (head + "\n" + "\n".join(combo_status_line(c) for c in oc)) if oc else ""

    def _tids(self) -> str:
        """Ids of the trades a strategy event (TP1 / SL move / exit) applies to."""
        ids = [c.get("tid") or c["id"] for c in self.open_combos()]
        return ", ".join(ids) if ids else (getattr(self, "cur_tid", "") or "-")

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
            if ev.kind == "ENTRY":
                self.cur_tid = trade_id(ev)
            tid = self.cur_tid if ev.kind == "ENTRY" else self._tids()
            self.storage.insert("events", {**ev.to_dict(), "mode": self.mode, "tid": tid,
                                           "logged_at": now_ist().isoformat()})
            if ev.kind == "ENTRY":
                self.handle_entry(ev)
            elif ev.kind == "EXIT":
                self.notify.send(M.exit_msg(tid, ev))
                for c in self.open_combos():
                    self._close(c, ev.reason)
            elif ev.kind == "SL_MOVE":
                old_sl = next((c.get("sl") for c in self.open_combos() if c.get("sl") is not None), None)
                self.notify.send(M.sl_move_msg(tid, old_sl, ev.sl, getattr(self.engine, "entryPrice", None)))
                for c in self.open_combos():
                    c["sl"] = ev.sl
                    self.storage.upsert("combos", {"id": c["id"]}, {"sl": ev.sl})
            elif ev.kind == "TP1":
                self.notify.send(M.tp1_msg(tid, ev, getattr(self.engine, "entryPrice", None), getattr(self.engine, "slPrice", None)))
                for c in self.open_combos():
                    self.storage.upsert("combos", {"id": c["id"]}, {"tp1_hit": True, "tp1_at": now_ist().isoformat()})

    def handle_entry(self, ev: Event, late: bool = False):
        side = "BUY" if ev.direction == 1 else "SELL"
        icon = "🟢" if ev.direction == 1 else "🔴"
        tid = trade_id(ev)
        self.cur_tid = tid
        self.storage.insert("signals", {**ev.to_dict(), "mode": self.mode, "tid": tid})
        self.notify.send(M.signal_msg(tid, ev, str(self.S.strategy.strategy_version), late))
        n = now_ist()
        sf = self.S.safety
        if self.kill_active():
            return self._skip(ev, "Kill switch active", "⛔ Kill switch active - signal not traded")
        if not self._in_window(n):
            return self._skip(ev, "Outside entry time window", "⏸ Outside entry time window - signal not traded")
        if int(sf.get("max_trades_per_day") or 0) and len(self.today_combos()) >= int(sf.max_trades_per_day):
            return self._skip(ev, "Max trades per day reached", "⏸ Max trades per day reached - signal not traded")
        if float(sf.get("max_daily_loss_rs") or 0):
            realized = sum(sum((c.get("pnl") or {}).values()) for c in self.today_combos() if c.get("status") == "CLOSED")
            if realized <= -abs(float(sf.max_daily_loss_rs)):
                return self._skip(ev, "Max daily loss reached", "⏸ Max daily loss reached - signal not traded")
        # opposite combos (only possible when duplicate filter is off) are closed first
        for c in self.open_combos():
            if c["direction"] != ev.direction:
                self._close(c, "REVERSE")
        if self.open_combos() and not self.S.strategy.allowMultiTriggerEntries:
            return self._skip(ev, "Combo already open (multi-trigger entries off)", "Combo already open - not adding")
        res = self.open_hedge(ev) or {}
        self._log_action(ev, res.get("outcome", "TRADED"), res.get("reason", ""),
                         **{k: v for k, v in res.items() if k not in ("outcome", "reason")})

    # ------------------------------------------------------------------ per-signal decision log (shown in the dashboard)
    @staticmethod
    def sig_id(ev: Event, mode: str) -> str:
        return f"{ev.time}|{ev.direction}|{ev.trigger}|{mode}"

    def _log_action(self, ev: Event, outcome: str, reason: str = "", **extra):
        """What the bot did with a strategy signal: TRADED / SKIPPED / FAILED and why. Never blocks trading."""
        try:
            doc = {"id": self.sig_id(ev, self.mode), "tid": trade_id(ev), "signal_time": ev.time, "direction": ev.direction,
                   "trigger": ev.trigger, "mode": self.mode, "outcome": outcome, "reason": reason,
                   "decided_at": now_ist().isoformat(), **extra}
            self.storage.upsert("signal_actions", {"id": doc["id"]}, doc)
        except Exception:  # noqa
            log.exception("could not store signal action")

    def _skip(self, ev: Event, reason: str, msg: str):
        self._log_action(ev, "SKIPPED", reason)
        self.notify.send(f"[{trade_id(ev)}] {msg}")

    def open_hedge(self, ev: Event) -> dict:
        """Pick the option pair and trade it. Returns the decision for the signal log."""
        H = self.S.hedge
        expiry, note = choose_expiry(self.data.option_expiries(), H.expiry_mode, now_ist().date(),
                                     H.get("expiry_date", ""), bool(H.get("use_next_expiry_on_expiry_day", True)))
        if note:
            self.notify.send(f"⚠ {note}")
        if not expiry:
            self.notify.send("❌ No valid expiry found - trade skipped")
            return {"outcome": "SKIPPED", "reason": "No valid expiry found"}
        deadline = time.time() + float(H.get("max_rescan_sec", 60))
        pair, matches, scans, ref = None, [], 0, ev.price
        while True:
            ref = self.data.ltp([self.signal_inst]).get(self.signal_inst.key) or ev.price
            _, strikes = strike_window(ref, int(H.get("strike_step", 50)), int(H.get("strike_scan_width", 10)))
            chain = self.data.option_chain(expiry, strikes)
            pair, matches = find_pair(ev.direction, chain, ref, H)
            scans += 1
            if pair or time.time() >= deadline or self.stop_requested():
                break
            log.info("No pair (%s) yet, rescanning", range_text(H))
            time.sleep(float(H.get("rescan_interval_sec", 5)))
        base = {"expiry": expiry.isoformat(), "scan_ref_price": ref, "scan_rounds": scans,
                "wanted": range_text(H)}
        if not pair:
            self.notify.send(f"⚠ No option pair ({range_text(H)}) found for {expiry} after {scans} scans - trade skipped")
            return {**base, "outcome": "SKIPPED", "reason": f"No option pair matched ({range_text(H)})"}
        top = [{"buy": f"{int(m.buy.strike)} {m.buy.opt_type}", "buy_ltp": m.buy.ltp,
                "sell": f"{int(m.sell.strike)} {m.sell.opt_type}", "sell_ltp": m.sell.ltp, "net": round(m.net, 2)}
               for m in matches[:5]]
        base.update(candidates=pair.candidates, top_candidates=top)
        tid = trade_id(ev)
        self.notify.send(M.pair_msg(tid, pair.describe(), expiry, pair.candidates))
        combo = self.executor.open_combo(pair, expiry, {**ev.to_dict(), "mode": self.mode, "tid": tid})
        self.notify.send(M.combo_open_msg(tid, combo))
        ok = combo["status"] == "OPEN"
        return {**base, "outcome": "TRADED" if ok else "FAILED", "combo_id": combo["id"],
                "reason": "" if ok else "; ".join(f"{n}: {l.get('msg', '').strip()}" for n, l in combo["legs"].items())}

    def _close(self, combo: dict, reason: str):
        c = self.executor.close_combo(combo, reason)
        pnl = c.get("pnl", {})
        self.notify.send(M.combo_closed_msg(c.get("tid") or c["id"], c, reason))

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
                        self.notify.send(f"[{c.get('tid') or c['id']}] Nifty {ltp:.2f} hit SL {e.slPrice:.2f}")
                        self._close(c, "SL_TICK")
                    elif (d == 1 and ltp >= e.effective_target()) or (d == -1 and ltp <= e.effective_target()):
                        self.notify.send(f"[{c.get('tid') or c['id']}] Nifty {ltp:.2f} hit Target {e.effective_target():.2f}")
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
                         f"Strategy stats: {self.engine.stats()}\nOpen position: {self.engine.position()}"
                         + ("\n\nToday's trades:\n" + "\n".join(combo_status_line(c) for c in combos) if combos else "")
                         + self._open_lines("\n\nStill open - carried to next session:"))

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
