"""Places / squares off the 2-leg option combo on every enabled broker."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List

from ..brokers.base import BrokerError, Instrument, round_tick
from ..data.candle import now_ist
from .scanner import HedgePair

log = logging.getLogger("executor")


@dataclass
class Fill:
    filled: int = 0
    avg: float = 0.0
    order_ids: List[str] = field(default_factory=list)
    msg: str = ""


class ComboExecutor:
    def __init__(self, brokers: Dict[str, object], bcfg, storage, notifier):
        self.brokers = brokers                  # name -> broker (real or paper)
        self.cfg = bcfg
        self.storage = storage
        self.notifier = notifier

    # ------------------------------------------------------------------ order filling
    def _fill(self, broker, inst: Instrument, side: str, qty: int, exiting: bool = False) -> Fill:
        cfg = self.cfg
        order_type = cfg.get("order_type", "LIMIT")
        buffer = float(cfg.get("limit_buffer_pts", 2.0))
        timeout = float(cfg.get("leg_fill_timeout_sec", 10))
        retries = int(cfg.get("leg_retry_count", 3))
        if exiting:
            retries = max(retries * 3, 6)
        freeze = int(cfg.get("freeze_qty", 1755))
        if inst.freeze_qty:
            freeze = min(freeze, inst.freeze_qty)
        lot = max(inst.lot_size, 1)
        max_slice = max((freeze // lot) * lot, lot)
        product = cfg.get("product", "NRML")
        res = Fill()
        value = 0.0
        remaining = qty
        while remaining > 0:
            slice_left = min(remaining, max_slice)
            for attempt in range(retries + 1):
                if slice_left <= 0:
                    break
                price = 0.0
                if order_type == "LIMIT":
                    ltp = broker.ltp([inst]).get(inst.key)
                    if not ltp:
                        res.msg = f"no LTP for {inst.symbol}"
                        time.sleep(1)
                        continue
                    step = buffer * (1 + attempt)  # widen on every retry
                    price = round_tick(ltp + step if side == "BUY" else max(ltp - step, 0.05))
                try:
                    oid = broker.place_order(inst, side, slice_left, order_type, price, product, tag="nexus-smc")
                except BrokerError as e:
                    res.msg = str(e)
                    log.error("%s place %s %s x%d failed: %s", broker.name, side, inst.symbol, slice_left, e)
                    time.sleep(1)
                    continue
                res.order_ids.append(oid)
                st = None
                t_end = time.time() + timeout
                while time.time() < t_end:
                    st = broker.order_status(oid)
                    if st.status in ("COMPLETE", "REJECTED", "CANCELLED"):
                        break
                    time.sleep(0.5)
                if st is None or st.status == "OPEN":
                    broker.cancel_order(oid)
                    time.sleep(0.5)
                    st = broker.order_status(oid)
                if st.filled_qty:
                    value += st.filled_qty * st.avg_price
                    res.filled += st.filled_qty
                    slice_left -= st.filled_qty
                    remaining -= st.filled_qty
                if st.status == "REJECTED":
                    res.msg = st.message
                    log.error("%s %s %s rejected: %s", broker.name, side, inst.symbol, st.message)
            if slice_left > 0:
                break  # could not complete this slice
        res.avg = round(value / res.filled, 2) if res.filled else 0.0
        return res

    # ------------------------------------------------------------------ open
    def new_combo(self, pair: HedgePair, expiry, signal: dict) -> dict:
        d = pair.direction
        return {
            "id": now_ist().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
            "status": "OPENING", "direction": d, "side": "BUY" if d == 1 else "SELL",
            "mode": signal.get("mode", ""),
            "trigger": signal.get("trigger"), "signal_time": signal.get("time"),
            "nifty_entry": signal.get("price"), "sl": signal.get("sl"), "target": signal.get("target"),
            "tp1": signal.get("tp1"), "rr": signal.get("rr"), "expiry": expiry.isoformat(),
            "buy": {"strike": pair.buy.strike, "type": pair.buy.opt_type, "scan_ltp": pair.buy.ltp},
            "sell": {"strike": pair.sell.strike, "type": pair.sell.opt_type, "scan_ltp": pair.sell.ltp},
            "scan_diff": pair.diff, "ref_price": pair.ref_price, "legs": {},
            "opened_at": now_ist().isoformat(),
        }

    def _open_on_broker(self, name: str, broker, combo: dict, expiry) -> dict:
        b, s = combo["buy"], combo["sell"]
        buy_inst = broker.option_instrument(expiry, b["strike"], b["type"])
        sell_inst = broker.option_instrument(expiry, s["strike"], s["type"])
        leg = {"status": "FAILED", "buy": {}, "sell": {}, "msg": ""}
        if not buy_inst or not sell_inst:
            leg["msg"] = "instrument not found on broker"
            return leg
        lots = int((self.cfg.get("lots") or {}).get(name.replace("paper-", ""), 1))
        qty = lots * buy_inst.lot_size
        leg["buy"] = {"symbol": buy_inst.symbol, "key": buy_inst.key, "lot_size": buy_inst.lot_size}
        leg["sell"] = {"symbol": sell_inst.symbol, "key": sell_inst.key, "lot_size": sell_inst.lot_size}

        if self.cfg.get("leg_order", "buy_first") == "simultaneous":
            out: Dict[str, Fill] = {}
            t1 = threading.Thread(target=lambda: out.__setitem__("b", self._fill(broker, buy_inst, "BUY", qty)))
            t2 = threading.Thread(target=lambda: out.__setitem__("s", self._fill(broker, sell_inst, "SELL", qty)))
            t1.start(); t2.start(); t1.join(); t2.join()
            fb, fs = out["b"], out["s"]
        else:
            fb = self._fill(broker, buy_inst, "BUY", qty)
            if fb.filled == 0:
                leg["msg"] = f"buy leg failed: {fb.msg}"
                leg["buy"].update(qty=0, entry=0, orders=fb.order_ids)
                return leg
            fs = self._fill(broker, sell_inst, "SELL", fb.filled)

        # equalise quantities (unwind any excess on one side)
        if fb.filled > fs.filled:
            ex = self._fill(broker, buy_inst, "SELL", fb.filled - fs.filled, exiting=True)
            leg["msg"] += f" unwound {ex.filled} excess buy qty;"
            fb.filled -= ex.filled
        elif fs.filled > fb.filled:
            ex = self._fill(broker, sell_inst, "BUY", fs.filled - fb.filled, exiting=True)
            leg["msg"] += f" unwound {ex.filled} excess sell qty;"
            fs.filled -= ex.filled
        leg["buy"].update(qty=fb.filled, entry=fb.avg, orders=fb.order_ids)
        leg["sell"].update(qty=fs.filled, entry=fs.avg, orders=fs.order_ids)
        if fb.filled > 0 and fb.filled == fs.filled:
            leg["status"] = "OPEN" if fb.filled == qty else "OPEN_PARTIAL"
        else:
            leg["status"] = "FAILED"
            leg["msg"] += f" sell leg failed: {fs.msg}"
        return leg

    def open_combo(self, pair: HedgePair, expiry, signal: dict) -> dict:
        combo = self.new_combo(pair, expiry, signal)
        results: Dict[str, dict] = {}
        threads = []
        for name, br in self.brokers.items():
            t = threading.Thread(target=lambda n=name, b=br: results.__setitem__(n, self._open_on_broker(n, b, combo, expiry)))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        combo["legs"] = results
        ok = [n for n, r in results.items() if r["status"].startswith("OPEN")]
        combo["status"] = "OPEN" if ok else "FAILED"
        self.storage.upsert("combos", {"id": combo["id"]}, combo)
        return combo

    # ------------------------------------------------------------------ close
    @staticmethod
    def _book_exit(side_leg: dict, f: Fill):
        done = side_leg.get("exit_qty", 0)
        value = side_leg.get("exit", 0.0) * done + f.avg * f.filled
        done += f.filled
        side_leg["exit_qty"] = done
        side_leg["exit"] = round(value / done, 2) if done else 0.0
        side_leg["exit_orders"] = side_leg.get("exit_orders", []) + f.order_ids

    def _close_on_broker(self, name, broker, leg: dict) -> dict:
        if leg.get("status") not in ("OPEN", "OPEN_PARTIAL", "CLOSE_FAILED"):
            return leg
        b, s = leg["buy"], leg["sell"]
        sell_inst = Instrument(s["key"], s["symbol"], None, None, "", s.get("lot_size", 1))
        buy_inst = Instrument(b["key"], b["symbol"], None, None, "", b.get("lot_size", 1))
        s_rem = s.get("qty", 0) - s.get("exit_qty", 0)
        b_rem = b.get("qty", 0) - b.get("exit_qty", 0)
        # 1) buy back the short leg first (never leave a naked short), 2) then sell the long leg
        if s_rem > 0:
            self._book_exit(s, self._fill(broker, sell_inst, "BUY", s_rem, exiting=True))
        if b_rem > 0:
            self._book_exit(b, self._fill(broker, buy_inst, "SELL", b_rem, exiting=True))
        if s.get("exit_qty", 0) >= s.get("qty", 0) and b.get("exit_qty", 0) >= b.get("qty", 0):
            leg["status"] = "CLOSED"
            leg["pnl"] = round((b["exit"] - b["entry"]) * b["qty"] + (s["entry"] - s["exit"]) * s["qty"], 2)
        else:
            leg["status"] = "CLOSE_FAILED"
            leg["msg"] = (f"exit incomplete (short {s.get('exit_qty', 0)}/{s.get('qty')}, "
                          f"long {b.get('exit_qty', 0)}/{b.get('qty')})")
        return leg

    def close_combo(self, combo: dict, reason: str) -> dict:
        threads = []
        for name, leg in combo["legs"].items():
            br = self.brokers.get(name)
            if br is None:
                continue
            t = threading.Thread(target=lambda n=name, b=br, l=leg: self._close_on_broker(n, b, l))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        bad = [n for n, l in combo["legs"].items() if l.get("status") == "CLOSE_FAILED"]
        combo["status"] = "CLOSE_FAILED" if bad else "CLOSED"
        combo["exit_reason"] = reason
        combo["closed_at"] = now_ist().isoformat()
        combo["pnl"] = {n: l.get("pnl", 0) for n, l in combo["legs"].items()}
        self.storage.upsert("combos", {"id": combo["id"]}, combo)
        return combo

    # ------------------------------------------------------------------ live P&L (for rupee SL/target)
    def combo_mtm(self, combo: dict, data_broker) -> Dict[str, float]:
        out = {}
        for name, leg in combo["legs"].items():
            if not leg.get("status", "").startswith("OPEN"):
                continue
            br = self.brokers.get(name)
            b, s = leg["buy"], leg["sell"]
            insts = [Instrument(b["key"], b["symbol"], None, None, ""), Instrument(s["key"], s["symbol"], None, None, "")]
            px = br.ltp(insts)
            if b["key"] in px and s["key"] in px:
                out[name] = (px[b["key"]] - b["entry"]) * b["qty"] + (s["entry"] - px[s["key"]]) * s["qty"]
        return out
