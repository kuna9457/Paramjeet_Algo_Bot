"""Common broker interface + shared types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional

from ..data.candle import Candle


@dataclass
class Instrument:
    key: str                  # broker-specific id (Upstox instrument_key / Kite "NFO:SYMBOL")
    symbol: str               # trading symbol
    expiry: Optional[date]
    strike: Optional[float]
    opt_type: str             # CE | PE | FUT | IDX
    lot_size: int = 1
    token: Optional[int] = None   # Kite instrument_token
    freeze_qty: int = 0           # exchange freeze quantity (0 = use settings value)


@dataclass
class OptionQuote:
    strike: float
    opt_type: str             # CE | PE
    ltp: float
    instrument: Instrument


@dataclass
class OrderResult:
    status: str               # COMPLETE | REJECTED | CANCELLED | OPEN
    filled_qty: int = 0
    avg_price: float = 0.0
    message: str = ""


class BrokerError(Exception):
    pass


class Broker(ABC):
    name: str = "base"

    # ---------- session
    @abstractmethod
    def connect(self) -> None: ...

    # ---------- reference data
    @abstractmethod
    def load_instruments(self) -> None: ...

    @abstractmethod
    def signal_instrument(self, signal_symbol: str) -> Instrument: ...

    @abstractmethod
    def option_expiries(self) -> List[date]: ...

    @abstractmethod
    def option_instrument(self, expiry: date, strike: float, opt_type: str) -> Optional[Instrument]: ...

    # ---------- market data
    @abstractmethod
    def candles_1m(self, inst: Instrument, start: datetime, end: datetime) -> List[Candle]: ...

    @abstractmethod
    def candles_1m_today(self, inst: Instrument) -> List[Candle]: ...

    @abstractmethod
    def ltp(self, insts: List[Instrument]) -> Dict[str, float]: ...

    def option_chain(self, expiry: date, strikes: List[float]) -> List[OptionQuote]:
        """Default: build from instruments + ltp(). Brokers may override with a native chain API."""
        insts = []
        for k in strikes:
            for t in ("CE", "PE"):
                i = self.option_instrument(expiry, k, t)
                if i:
                    insts.append(i)
        prices = self.ltp(insts)
        return [OptionQuote(i.strike, i.opt_type, prices[i.key], i) for i in insts if prices.get(i.key)]

    # ---------- orders
    @abstractmethod
    def place_order(self, inst: Instrument, side: str, qty: int, order_type: str, price: float,
                    product: str, tag: str = "") -> str: ...

    @abstractmethod
    def order_status(self, order_id: str) -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> None: ...


def round_tick(px: float, tick: float = 0.05) -> float:
    return round(round(px / tick) * tick, 2)
