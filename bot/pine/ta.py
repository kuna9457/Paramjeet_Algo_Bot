"""Incremental re-implementations of the Pine Script v5 built-ins used by the strategy.

`None` plays the role of Pine's `na`. Every class is fed one value per bar via update().
The maths follows TradingView's documented reference implementations (SMA-seeded RMA/EMA,
ta.tr, ta.dmi, ta.pivothigh/pivotlow) so values match the chart.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Optional, Sequence

na = None
Num = Optional[float]

# Pivot tie rule. TradingView treats the LEFT side strictly (a bar equal to the candidate
# on the left cancels the pivot) and allows equality on the RIGHT side. Verified in parity tests.
PIVOT_LEFT_STRICT = True
PIVOT_RIGHT_STRICT = False


def nz(x: Num, default: float = 0.0) -> float:
    return default if x is None else x


def is_na(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def nmax(*xs: Num) -> Num:
    """math.max – na if any argument is na."""
    if any(x is None for x in xs):
        return None
    return max(xs)


def nmin(*xs: Num) -> Num:
    if any(x is None for x in xs):
        return None
    return min(xs)


# --- comparisons that return False when either side is na (Pine bool behaviour) -------------
def gt(a: Num, b: Num) -> bool:
    return a is not None and b is not None and a > b


def ge(a: Num, b: Num) -> bool:
    return a is not None and b is not None and a >= b


def lt(a: Num, b: Num) -> bool:
    return a is not None and b is not None and a < b


def le(a: Num, b: Num) -> bool:
    return a is not None and b is not None and a <= b


def add(a: Num, b: Num) -> Num:
    return None if a is None or b is None else a + b


def sub(a: Num, b: Num) -> Num:
    return None if a is None or b is None else a - b


def mul(a: Num, b: Num) -> Num:
    return None if a is None or b is None else a * b


class _Seeded:
    """Recursive MA seeded with SMA of the first `length` values (Pine ta.rma / ta.ema)."""

    def __init__(self, length: int, alpha: float):
        self.length = length
        self.alpha = alpha
        self.window: deque = deque(maxlen=length)
        self.value: Num = None

    def update(self, x: Num) -> Num:
        self.window.append(x)
        if self.value is None:
            if len(self.window) == self.length and all(v is not None for v in self.window):
                self.value = sum(self.window) / self.length
            else:
                self.value = None
        else:
            self.value = None if x is None else self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class RMA(_Seeded):
    def __init__(self, length: int):
        super().__init__(length, 1.0 / length)


class EMA(_Seeded):
    def __init__(self, length: int):
        super().__init__(length, 2.0 / (length + 1))


class SumWindow:
    """math.sum(src, n) – na unless the last n values are all non-na."""

    def __init__(self, length: int):
        self.window: deque = deque(maxlen=length)
        self.length = length

    def update(self, x: Num) -> Num:
        self.window.append(x)
        if len(self.window) < self.length or any(v is None for v in self.window):
            return None
        return sum(self.window)


def true_range(high: float, low: float, prev_close: Num, handle_na: bool) -> Num:
    if prev_close is None:
        return (high - low) if handle_na else None
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


class ATR:
    """ta.atr(length) = ta.rma(ta.tr(true), length)."""

    def __init__(self, length: int):
        self.rma = RMA(length)

    def update(self, high: float, low: float, prev_close: Num) -> Num:
        return self.rma.update(true_range(high, low, prev_close, True))


class DMI:
    """ta.dmi(diLength, adxSmoothing) -> (plus, minus, adx)."""

    def __init__(self, di_len: int, adx_len: int):
        self.tr_rma = RMA(di_len)
        self.plus_rma = RMA(di_len)
        self.minus_rma = RMA(di_len)
        self.adx_rma = RMA(adx_len)
        self.prev_high: Num = None
        self.prev_low: Num = None
        self.last_plus: Num = None   # fixnan memory
        self.last_minus: Num = None

    def update(self, high: float, low: float, prev_close: Num):
        up = None if self.prev_high is None else high - self.prev_high
        down = None if self.prev_low is None else -(low - self.prev_low)
        plus_dm = None if up is None else (up if (up > down and up > 0) else 0.0)
        minus_dm = None if down is None else (down if (down > up and down > 0) else 0.0)
        trur = self.tr_rma.update(true_range(high, low, prev_close, False))
        p = self.plus_rma.update(plus_dm)
        m = self.minus_rma.update(minus_dm)
        plus = None if (p is None or trur is None or trur == 0) else 100 * p / trur
        minus = None if (m is None or trur is None or trur == 0) else 100 * m / trur
        if plus is None:
            plus = self.last_plus
        else:
            self.last_plus = plus
        if minus is None:
            minus = self.last_minus
        else:
            self.last_minus = minus
        if plus is None or minus is None:
            adx = self.adx_rma.update(None)
        else:
            s = plus + minus
            adx = self.adx_rma.update(abs(plus - minus) / (1 if s == 0 else s))
            adx = None if adx is None else 100 * adx
        self.prev_high, self.prev_low = high, low
        return plus, minus, adx


def highest(series: Sequence[float], length: int) -> Num:
    """ta.highest(src, length) evaluated on the current (last) bar of `series`."""
    if len(series) < length:
        return None
    return max(series[-length:])


def lowest(series: Sequence[float], length: int) -> Num:
    if len(series) < length:
        return None
    return min(series[-length:])


def pivot_high(series: Sequence[float], left: int, right: int) -> Num:
    """ta.pivothigh(src, left, right) on the current bar: returns src[right] if it is a pivot."""
    n = len(series)
    if n < left + right + 1:
        return None
    c = n - 1 - right
    v = series[c]
    for j in range(c - left, c):
        if series[j] > v or (PIVOT_LEFT_STRICT and series[j] == v):
            return None
    for j in range(c + 1, n):
        if series[j] > v or (PIVOT_RIGHT_STRICT and series[j] == v):
            return None
    return v


def pivot_low(series: Sequence[float], left: int, right: int) -> Num:
    n = len(series)
    if n < left + right + 1:
        return None
    c = n - 1 - right
    v = series[c]
    for j in range(c - left, c):
        if series[j] < v or (PIVOT_LEFT_STRICT and series[j] == v):
            return None
    for j in range(c + 1, n):
        if series[j] < v or (PIVOT_RIGHT_STRICT and series[j] == v):
            return None
    return v
