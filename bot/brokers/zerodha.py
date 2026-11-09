"""Zerodha Kite Connect connector."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from kiteconnect import KiteConnect

from ..data.candle import Candle, now_ist, to_ist, session_open_dt
from .base import Broker, BrokerError, Instrument, OrderResult

log = logging.getLogger("zerodha")
NIFTY_INDEX_TOKEN = 256265


class ZerodhaBroker(Broker):
    name = "zerodha"

    def __init__(self, api_key: str, access_token: str):
        if not api_key or not access_token:
            raise BrokerError("Kite api key / access token missing. Run: python main.py login zerodha")
        self.kc = KiteConnect(api_key=api_key)
        self.kc.set_access_token(access_token)
        self.options: Dict[tuple, Instrument] = {}
        self.futures: List[Instrument] = []
        self._expiries: List[date] = []

    def _call(self, fn, *a, **kw):
        for attempt in range(3):
            try:
                return fn(*a, **kw)
            except Exception as e:  # kiteconnect raises many exception types
                msg = str(e)
                if "Too many requests" in msg and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise BrokerError(f"Kite: {msg}")

    def connect(self) -> None:
        self._call(self.kc.profile)
        log.info("Kite connected")

    @staticmethod
    def login_url(api_key: str) -> str:
        return KiteConnect(api_key=api_key).login_url()

    @staticmethod
    def exchange_request_token(api_key: str, api_secret: str, request_token: str) -> str:
        kc = KiteConnect(api_key=api_key)
        return kc.generate_session(request_token, api_secret=api_secret)["access_token"]

    # ------------------------------------------------------------------ instruments
    def load_instruments(self) -> None:
        rows = self._call(self.kc.instruments, "NFO")
        self.options.clear()
        self.futures.clear()
        exp = set()
        for x in rows:
            if x.get("name") != "NIFTY":
                continue
            t = x.get("instrument_type")
            inst = Instrument(key=f"NFO:{x['tradingsymbol']}", symbol=x["tradingsymbol"], expiry=x.get("expiry"),
                              strike=float(x.get("strike") or 0), opt_type=t, lot_size=int(x.get("lot_size") or 1),
                              token=int(x["instrument_token"]))
            if t in ("CE", "PE"):
                self.options[(inst.expiry, inst.strike, t)] = inst
                exp.add(inst.expiry)
            elif t == "FUT":
                self.futures.append(inst)
        self.futures.sort(key=lambda i: i.expiry)
        self._expiries = sorted(exp)
        log.info("Kite instruments: %d NIFTY options, %d futures", len(self.options), len(self.futures))

    def signal_instrument(self, signal_symbol: str) -> Instrument:
        if signal_symbol == "NIFTY_FUT":
            today = now_ist().date()
            for f in self.futures:
                if f.expiry >= today:
                    return f
            raise BrokerError("No live NIFTY future found")
        return Instrument("NSE:NIFTY 50", "NIFTY 50", None, None, "IDX", token=NIFTY_INDEX_TOKEN)

    def option_expiries(self) -> List[date]:
        return list(self._expiries)

    def option_instrument(self, expiry, strike, opt_type) -> Optional[Instrument]:
        return self.options.get((expiry, float(strike), opt_type))

    # ------------------------------------------------------------------ data
    def candles_1m(self, inst: Instrument, start: datetime, end: datetime) -> List[Candle]:
        out: Dict[datetime, Candle] = {}
        cur_end = end
        while cur_end > start:
            cur_start = max(start, cur_end - timedelta(days=55))
            rows = self._call(self.kc.historical_data, inst.token, cur_start.replace(tzinfo=None),
                              cur_end.replace(tzinfo=None), "minute")
            for r in rows:
                t = to_ist(r["date"])
                out[t] = Candle(t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                                float(r.get("volume") or 0))
            cur_end = cur_start
            time.sleep(0.4)
        return [out[k] for k in sorted(out)]

    def candles_1m_today(self, inst: Instrument) -> List[Candle]:
        n = now_ist()
        return self.candles_1m(inst, session_open_dt(n), n)

    def ltp(self, insts: List[Instrument]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for i in range(0, len(insts), 500):
            chunk = [x.key for x in insts[i:i + 500]]
            data = self._call(self.kc.ltp, chunk)
            for k, v in data.items():
                out[k] = float(v.get("last_price") or 0)
        return out

    # ------------------------------------------------------------------ orders
    def place_order(self, inst, side, qty, order_type, price, product, tag=""):
        kw = dict(variety=self.kc.VARIETY_REGULAR, exchange="NFO", tradingsymbol=inst.symbol,
                  transaction_type=side, quantity=int(qty), product=product, order_type=order_type,
                  tag=(tag or "nexus")[:20])
        if order_type == "LIMIT":
            kw["price"] = float(price)
        else:
            kw["market_protection"] = -1  # required by Kite for API market orders
        try:
            return str(self._call(self.kc.place_order, **kw))
        except BrokerError:
            if "market_protection" in kw:
                kw.pop("market_protection")
                return str(self._call(self.kc.place_order, **kw))
            raise

    def order_status(self, order_id: str) -> OrderResult:
        hist = self._call(self.kc.order_history, order_id)
        if not hist:
            return OrderResult("OPEN")
        d = hist[-1]
        st = (d.get("status") or "").upper()
        status = st if st in ("COMPLETE", "REJECTED", "CANCELLED") else "OPEN"
        return OrderResult(status, int(d.get("filled_quantity") or 0), float(d.get("average_price") or 0),
                           d.get("status_message") or "")

    def cancel_order(self, order_id: str) -> None:
        try:
            self._call(self.kc.cancel_order, self.kc.VARIETY_REGULAR, order_id)
        except BrokerError as e:
            log.warning("cancel %s: %s", order_id, e)
