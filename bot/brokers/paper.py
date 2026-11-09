"""Paper broker: uses a real data broker for prices, fills orders instantly at LTP."""
from __future__ import annotations

import uuid
from typing import Dict

from .base import Broker, Instrument, OrderResult


class PaperBroker:
    """Wraps a data broker. Exposes the order API of Broker; reference/market data is delegated."""

    def __init__(self, data_broker: Broker, label: str):
        self.data = data_broker
        self.name = f"paper-{label}"
        self.orders: Dict[str, OrderResult] = {}
        self.price_override: Dict[str, float] = {}   # tests can inject prices

    def __getattr__(self, item):
        return getattr(self.data, item)

    def option_instrument(self, expiry, strike, opt_type):
        return self.data.option_instrument(expiry, strike, opt_type)

    def place_order(self, inst: Instrument, side, qty, order_type, price, product, tag=""):
        px = self.price_override.get(inst.key)
        if px is None:
            px = self.data.ltp([inst]).get(inst.key) or price
        oid = "P-" + uuid.uuid4().hex[:10]
        self.orders[oid] = OrderResult("COMPLETE", int(qty), float(px), "paper fill")
        return oid

    def order_status(self, order_id):
        return self.orders.get(order_id, OrderResult("REJECTED", message="unknown"))

    def cancel_order(self, order_id):
        return None
