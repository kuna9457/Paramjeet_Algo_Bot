"""Upstox API v2/v3 connector (plain REST, no SDK dependency)."""
from __future__ import annotations

import gzip
import io
import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

from ..data.candle import IST, Candle, now_ist, to_ist
from .base import Broker, BrokerError, Instrument, OptionQuote, OrderResult

log = logging.getLogger("upstox")

API = "https://api.upstox.com"
HFT = "https://api-hft.upstox.com"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"


class UpstoxBroker(Broker):
    name = "upstox"

    def __init__(self, access_token: str):
        if not access_token:
            raise BrokerError("Upstox access token missing. Run: python main.py login upstox")
        self.token = access_token
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                               "Content-Type": "application/json"})
        self.options: Dict[tuple, Instrument] = {}
        self.futures: List[Instrument] = []
        self._expiries: List[date] = []

    # ------------------------------------------------------------------ http
    def _req(self, method: str, url: str, **kw):
        for attempt in range(3):
            try:
                r = self.s.request(method, url, timeout=15, **kw)
            except requests.RequestException as e:
                if attempt == 2:
                    raise BrokerError(f"Upstox network error: {e}")
                time.sleep(1)
                continue
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            try:
                body = r.json()
            except ValueError:
                raise BrokerError(f"Upstox bad response {r.status_code}: {r.text[:200]}")
            if r.status_code >= 400 or body.get("status") == "error":
                raise BrokerError(f"Upstox error {r.status_code}: {body.get('errors') or body}")
            return body
        raise BrokerError("Upstox rate limited")

    # ------------------------------------------------------------------ session
    def connect(self) -> None:
        self._req("GET", f"{API}/v2/user/profile")
        log.info("Upstox connected")

    @staticmethod
    def exchange_code(api_key: str, api_secret: str, redirect_uri: str, code: str) -> str:
        r = requests.post(f"{API}/v2/login/authorization/token", timeout=15,
                          headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                          data={"code": code, "client_id": api_key, "client_secret": api_secret,
                                "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        body = r.json()
        if "access_token" not in body:
            raise BrokerError(f"Upstox login failed: {body}")
        return body["access_token"]

    @staticmethod
    def login_url(api_key: str, redirect_uri: str) -> str:
        return (f"{API}/v2/login/authorization/dialog?response_type=code&client_id={api_key}"
                f"&redirect_uri={quote(redirect_uri, safe='')}")

    # ------------------------------------------------------------------ instruments
    def load_instruments(self) -> None:
        r = requests.get(INSTRUMENTS_URL, timeout=60)
        r.raise_for_status()
        rows = json.load(gzip.GzipFile(fileobj=io.BytesIO(r.content)))
        self.options.clear()
        self.futures.clear()
        exp = set()
        for x in rows:
            if x.get("segment") != "NSE_FO":
                continue
            und = x.get("underlying_symbol") or x.get("asset_symbol") or x.get("name")
            if und != "NIFTY":
                continue
            itype = x.get("instrument_type")
            ex = x.get("expiry")
            exd = datetime.fromtimestamp(ex / 1000, IST).date() if isinstance(ex, (int, float)) else None
            inst = Instrument(key=x["instrument_key"], symbol=x.get("trading_symbol", ""), expiry=exd,
                              strike=float(x.get("strike_price") or 0), opt_type=itype,
                              lot_size=int(x.get("lot_size") or 1),
                              freeze_qty=int(float(x.get("freeze_quantity") or 0)))
            if itype in ("CE", "PE"):
                self.options[(exd, inst.strike, itype)] = inst
                exp.add(exd)
            elif itype == "FUT":
                self.futures.append(inst)
        self.futures.sort(key=lambda i: i.expiry)
        self._expiries = sorted(exp)
        log.info("Upstox instruments: %d NIFTY options, %d futures", len(self.options), len(self.futures))

    def signal_instrument(self, signal_symbol: str) -> Instrument:
        if signal_symbol == "NIFTY_FUT":
            today = now_ist().date()
            for f in self.futures:
                if f.expiry >= today:
                    return f
            raise BrokerError("No live NIFTY future found")
        return Instrument(NIFTY_INDEX_KEY, "NIFTY 50", None, None, "IDX")

    def option_expiries(self) -> List[date]:
        return list(self._expiries)

    def option_instrument(self, expiry: date, strike: float, opt_type: str) -> Optional[Instrument]:
        return self.options.get((expiry, float(strike), opt_type))

    # ------------------------------------------------------------------ market data
    @staticmethod
    def _parse_candles(body) -> List[Candle]:
        out = []
        for row in (body.get("data") or {}).get("candles") or []:
            t = to_ist(datetime.fromisoformat(row[0]))
            out.append(Candle(t, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5] or 0)))
        out.sort(key=lambda c: c.time)
        return out

    def candles_1m(self, inst: Instrument, start: datetime, end: datetime) -> List[Candle]:
        key = quote(inst.key, safe="")
        out: Dict[datetime, Candle] = {}
        cur_end = end.date()
        start_d = start.date()
        while cur_end >= start_d:
            cur_start = max(start_d, cur_end - timedelta(days=27))
            url = f"{API}/v3/historical-candle/{key}/minutes/1/{cur_end.isoformat()}/{cur_start.isoformat()}"
            for c in self._parse_candles(self._req("GET", url)):
                out[c.time] = c
            cur_end = cur_start - timedelta(days=1)
            time.sleep(0.3)
        if end.date() >= now_ist().date():
            for c in self.candles_1m_today(inst):
                out[c.time] = c
        return [out[k] for k in sorted(out) if start <= k <= end]

    def candles_1m_today(self, inst: Instrument) -> List[Candle]:
        key = quote(inst.key, safe="")
        return self._parse_candles(self._req("GET", f"{API}/v3/historical-candle/intraday/{key}/minutes/1"))

    def ltp(self, insts: List[Instrument]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for i in range(0, len(insts), 450):
            chunk = insts[i:i + 450]
            keys = ",".join(x.key for x in chunk)
            body = self._req("GET", f"{API}/v2/market-quote/ltp", params={"instrument_key": keys})
            for _, v in (body.get("data") or {}).items():
                tok = v.get("instrument_token")
                if tok:
                    out[tok] = float(v.get("last_price") or 0)
        return out

    def option_chain(self, expiry: date, strikes: List[float]) -> List[OptionQuote]:
        body = self._req("GET", f"{API}/v2/option/chain",
                         params={"instrument_key": NIFTY_INDEX_KEY, "expiry_date": expiry.isoformat()})
        want = set(float(k) for k in strikes)
        res: List[OptionQuote] = []
        for row in body.get("data") or []:
            k = float(row.get("strike_price"))
            if k not in want:
                continue
            for side, t in (("call_options", "CE"), ("put_options", "PE")):
                o = row.get(side) or {}
                md = o.get("market_data") or {}
                ltp = float(md.get("ltp") or 0)
                inst = self.option_instrument(expiry, k, t) or Instrument(o.get("instrument_key", ""), "", expiry, k, t)
                if ltp > 0 and inst.key:
                    res.append(OptionQuote(k, t, ltp, inst))
        return res

    # ------------------------------------------------------------------ orders
    def place_order(self, inst, side, qty, order_type, price, product, tag=""):
        payload = {
            "quantity": int(qty), "product": "D" if product == "NRML" else "I", "validity": "DAY",
            "price": float(price) if order_type == "LIMIT" else 0, "tag": tag[:20] or "nexus",
            "instrument_token": inst.key, "order_type": order_type, "transaction_type": side,
            "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False,
        }
        body = self._req("POST", f"{HFT}/v2/order/place", data=json.dumps(payload))
        return str(body["data"]["order_id"])

    def order_status(self, order_id: str) -> OrderResult:
        body = self._req("GET", f"{API}/v2/order/details", params={"order_id": order_id})
        d = body.get("data") or {}
        st = (d.get("status") or "").lower()
        if st == "complete":
            status = "COMPLETE"
        elif st == "rejected":
            status = "REJECTED"
        elif st == "cancelled":
            status = "CANCELLED"
        else:
            status = "OPEN"
        return OrderResult(status, int(d.get("filled_quantity") or 0), float(d.get("average_price") or 0),
                           d.get("status_message") or "")

    def cancel_order(self, order_id: str) -> None:
        try:
            self._req("DELETE", f"{HFT}/v2/order/cancel", params={"order_id": order_id})
        except BrokerError as e:
            log.warning("cancel %s: %s", order_id, e)

    def funds(self) -> float:
        body = self._req("GET", f"{API}/v2/user/get-funds-and-margin", params={"segment": "SEC"})
        return float(((body.get("data") or {}).get("equity") or {}).get("available_margin") or 0)
