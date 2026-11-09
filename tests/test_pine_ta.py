from datetime import datetime, timedelta

from bot.data.candle import IST, Candle, bucket_bounds, resample
from bot.pine.mtf import HTFContext
from bot.pine.ta import ATR, EMA, RMA, pivot_high, pivot_low


def test_rma_ema_seeded_with_sma():
    xs = [1, 2, 3, 4, 5, 6, 7, 8]
    r, e = RMA(3), EMA(3)
    rv = [r.update(x) for x in xs]
    ev = [e.update(x) for x in xs]
    assert rv[:2] == [None, None] and ev[:2] == [None, None]
    assert rv[2] == 2.0 and ev[2] == 2.0            # SMA seed
    assert abs(rv[3] - (4 / 3 + 2 * 2 / 3)) < 1e-9  # alpha = 1/3
    assert abs(ev[3] - (0.5 * 4 + 0.5 * 2)) < 1e-9  # alpha = 2/(3+1)


def test_atr_first_bar_uses_high_low():
    a = ATR(2)
    assert a.update(10, 8, None) is None
    v = a.update(12, 9, 9)          # tr = max(3, 3, 0) = 3 ; seed = (2+3)/2
    assert v == 2.5


def test_pivots_need_full_window_and_tie_rules():
    s = [1, 2, 5, 3, 2]
    assert pivot_high(s, 2, 2) == 5
    assert pivot_high(s[:-1], 2, 2) is None          # not enough right bars
    assert pivot_high([1, 5, 5, 3, 2], 2, 2) is None  # equal on the left cancels
    assert pivot_high([1, 2, 5, 5, 2], 2, 2) == 5    # equal on the right allowed
    assert pivot_low([5, 4, 1, 3, 4], 2, 2) == 1


def _c(t, o, h, l, c):
    return Candle(t, o, h, l, c)


def test_resample_anchored_to_0915_and_last_hour_cut():
    t0 = datetime(2025, 1, 6, 9, 15, tzinfo=IST)
    m1 = [_c(t0 + timedelta(minutes=i), 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(375)]
    h1 = resample(m1, 60)
    assert [b.time.strftime("%H:%M") for b in h1] == ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]
    assert bucket_bounds(h1[-1].time, 60)[1].strftime("%H:%M") == "15:30"
    m5 = resample(m1, 5)
    assert len(m5) == 75 and m5[0].open == 100 and m5[0].close == 104.5 and m5[0].high == 105


def test_htf_value_visible_on_last_chart_bar_of_htf_bar():
    t0 = datetime(2025, 1, 6, 9, 15, tzinfo=IST)
    ctx = HTFContext(15)
    closes = []
    for i in range(9):  # 9 x 5m = three 15m bars
        b = _c(t0 + timedelta(minutes=5 * i), 1, 2, 0, float(i))
        ctx.on_chart_bar(b, 5)
        closes.append(ctx.snap["close_1"])
    # close[1] of the 15m series: 2nd HTF bar completes at chart bar index 5 -> previous HTF close = 2
    assert closes == [None, None, None, None, None, 2.0, 2.0, 2.0, 5.0]
