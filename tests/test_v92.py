"""Trial v9.2 = v8.7 + ATR SL, 1.25R / 8-bar 0.5R exits, 0.55R break-even, 2-loss hold, new OB/structure rules.
v7.4 / v8.0 / v8.7 must stay byte-identical (their goldens are checked in test_v87.py)."""
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bot.config import StrategySettings, _build_strategy, load_preset
from bot.data.candle import IST, Candle, resample
from bot.strategy.engine import NexusEngine, Zone
from tests.baseline_v74 import M1, snapshot

V92_GOLDEN = Path(__file__).parent / "data" / "v92_golden.json"


def eng(version="v9.2", **kw):
    return NexusEngine(replace(StrategySettings(**load_preset("v9.2")), strategy_version=version, **kw))


def preset_run(tf=5):
    e = eng(chart_timeframe=tf)
    ev, holds = [], []
    for b in resample(M1, tf):
        out = e.on_bar(b)
        ev += out
        holds.append((e.diag["onHold"], [x.kind for x in out]))
    return e, ev, holds


def pinned() -> dict:
    out = snapshot("v9.2")
    for tf in (5, 15):
        out[f"preset@{tf}"] = [x.to_dict() for x in preset_run(tf)[1]]
    return json.loads(json.dumps(out, default=str))


def open_trade(e, d=1, entry=25900.0, sl_pts=90.0, bar=100):
    e.bar_index = bar
    e.activeTradeDir = d
    e.entryPrice = entry
    e.slPrice = entry - d * sl_pts
    e.initialRiskPoints = sl_pts
    e.targetPrice = entry + d * sl_pts * 1.25
    e.currentTradeRr = 1.25
    e.tp1Price = entry + d * sl_pts * 0.55
    e.tp1Hit = False
    e.tp1HitBarIndex = -100
    e.lastSignalBarIndex = bar


def step(e, d, o, h, l, c, held=1):
    e.bar_index = e.lastSignalBarIndex + held
    ev = []
    e._manage(d, False, o, h, l, c, max(e.initialRiskPoints, 10.0), "t", ev)
    return ev


# ------------------------------------------------------------------ versions / pinned output
def test_v92_flags_and_pinned_output():
    e = eng()
    assert (e.v8, e.v87, e.v92) == (True, True, True)
    assert eng("v8.7", slMode="Fixed Points (Preset)").v92 is False
    now = pinned()     # re-pin after an intended v9.2 change: V92_GOLDEN.write_text(json.dumps(pinned()))
    assert now == json.loads(V92_GOLDEN.read_text())
    assert now["defaults@5"] != snapshot("v8.7")["defaults@5"]


def test_v92_preset_matches_pine_inputs():
    p = load_preset("v9.2")
    assert p["slMode"] == "ATR Adaptive (3.0x)"
    assert (p["minTargetRr"], p["tp1RrMultiple"], p["adxThreshold"], p["signalCooldownBars"]) == (1.25, 0.55, 24.0, 8)
    assert (p["maxOrderBlocks"], p["maxFvgCount"]) == (12, 12)
    assert (p["enableTimeExit"], p["enableLossHold"], p["holdDurationHours"]) == (False, True, 2)
    rest = {k: v for k, v in load_preset("v8.7").items() if k not in p or p[k] != v}
    assert set(rest) == {"strategy_version", "slMode", "minTargetRr", "tp1RrMultiple", "adxThreshold",
                         "signalCooldownBars", "maxOrderBlocks", "maxFvgCount", "enableTimeExit", "enableLossHold"}


def test_atr_sl_mode_only_with_v92():
    assert _build_strategy({"strategy_version": "v9.2", "slMode": "ATR Adaptive (3.0x)"}, {}).slMode.startswith("ATR")
    with pytest.raises(ValueError):
        _build_strategy({"strategy_version": "v8.7", "slMode": "ATR Adaptive (3.0x)"}, {})
    with pytest.raises(ValueError):
        _build_strategy({"slMode": "ATR 2x"}, {})


# ------------------------------------------------------------------ SL / target / TP1 (client sheet numbers)
@pytest.mark.parametrize("atr, sl", [(10.0, 65.0), (30.0, 90.0), (40.0, 120.0), (60.0, 140.0)])
def test_atr_sl_is_3x_atr_clamped_65_to_140(atr, sl):
    e = eng()
    calcEntry, calcSl, slDist, target, rr = e._plan_trade(1, 25900.0, atr, False, None, None)
    assert slDist == sl and calcSl == 25900.0 - sl
    assert rr == pytest.approx(1.25) and target == pytest.approx(25900.0 + 1.25 * sl)   # 70-pt fallback < 1.25R
    s_entry, s_sl, *_ = e._plan_trade(-1, 25900.0, atr, False, None, None)
    assert s_sl == 25900.0 + sl


def test_client_example_buy_25900_sl_90():
    e = eng()
    _, sl, sl_dist, tgt, _ = e._plan_trade(1, 25900.0, 30.0, False, None, None)
    e._open(1, "OB", 25900.0, sl, sl_dist, tgt, 1.25, "t", [])
    assert (e.slPrice, e.targetPrice, e.tp1Price) == (25810.0, 26012.5, pytest.approx(25949.5))
    assert e.effective_target() == 26012.5            # no +70 pts shortcut in v9.2


# ------------------------------------------------------------------ exits
def test_eight_bar_exit_needs_close_at_half_r():
    e = eng(); open_trade(e)
    assert not [x for x in step(e, 1, 25940, 25950, 25930, 25946, held=7) if x.kind == "EXIT"]    # 7 bars
    ex = [x for x in step(e, 1, 25940, 25950, 25930, 25946, held=8) if x.kind == "EXIT"]
    assert ex and ex[0].reason == "DECAY_8" and ex[0].price == 25946
    e = eng(); open_trade(e)
    assert not [x for x in step(e, 1, 25940, 25950, 25930, 25944, held=8) if x.kind == "EXIT"]   # +44 < 0.5R


def test_no_plus_70_exit_and_target_at_1_25r():
    e = eng(); open_trade(e)
    assert not [x for x in step(e, 1, 25960, 25975, 25955, 25965) if x.kind == "EXIT"]   # +75 pts: still open
    ex = [x for x in step(e, 1, 25990, 26013, 25985, 26000, held=2) if x.kind == "EXIT"]
    assert ex[0].reason == "TARGET" and ex[0].price == 26012.5


def test_break_even_at_0_55r_from_next_bar_then_cost_to_cost_exit():
    e = eng(); open_trade(e)
    ev = step(e, 1, 25920, 25950, 25915, 25940)            # touches 25949.5
    assert [x.kind for x in ev] == ["TP1"] and e.slPrice == 25810.0
    ev = step(e, 1, 25940, 25945, 25920, 25930, held=2)
    assert any(x.kind == "SL_MOVE" and x.sl == 25900.0 for x in ev)
    ex = [x for x in step(e, 1, 25910, 25912, 25895, 25898, held=3) if x.kind == "EXIT"]
    assert ex[0].reason == "BE" and ex[0].price == 25900.0


def test_short_mirror_and_time_exit_off_by_default():
    e = eng(); open_trade(e, d=-1)
    assert (e.tp1Price, e.targetPrice) == (pytest.approx(25850.5), 25787.5)
    assert not [x for x in step(e, -1, 25880, 25890, 25870, 25880, held=400) if x.kind == "EXIT" and x.reason == "TIME"]
    e = eng(enableTimeExit=True); open_trade(e, d=-1)
    ex = [x for x in step(e, -1, 25880, 25890, 25870, 25880, held=150) if x.kind == "EXIT"]
    assert ex and ex[0].reason == "TIME"


# ------------------------------------------------------------------ 2-loss hold
def test_two_losses_in_a_row_pause_entries_for_two_hours():
    e = eng()
    t0 = datetime(2026, 11, 27, 10, 0, tzinfo=IST)
    e.last_bar_time = t0
    e._count_result(False)
    assert e.holdUntil is None
    e._count_result(False)
    assert e.consecutiveLosses == 2 and e.holdUntil == t0 + timedelta(hours=2)
    e._count_result(True)
    assert e.consecutiveLosses == 0
    off = eng(enableLossHold=False)
    off.last_bar_time = t0
    off._count_result(False); off._count_result(False)
    assert off.holdUntil is None


def test_sl_loss_counts_towards_the_hold_and_no_entry_while_paused():
    e = eng(); open_trade(e)
    e.last_bar_time = datetime(2026, 11, 27, 10, 0, tzinfo=IST)
    step(e, 1, 25850, 25860, 25800, 25805)
    assert e.consecutiveLosses == 1
    e, ev, holds = preset_run()
    assert any(h for h, _ in holds), "synthetic data should trigger at least one hold"
    assert not [k for h, k in holds if h and "ENTRY" in k]


# ------------------------------------------------------------------ zones
def test_live_zone_lists_capped_at_5_and_mitigated_zones_cleared():
    e, _, _ = preset_run()
    for lst in (e.bullObList, e.bearObList, e.bullFvgList, e.bearFvgList):
        assert len(lst) <= 5
        if len(lst) > 3:
            assert not any(z.mitigated for z in lst)
    e = eng(chart_timeframe=5)
    e.bullFvgList = [Zone(1, 0, True, mitigated=True), Zone(1, 0, True), Zone(1, 0, True), Zone(1, 0, True)]
    e.on_bar(Candle(datetime(2026, 11, 27, 9, 15, tzinfo=IST), 100, 101, 99, 100, 0))
    assert len(e.bullFvgList) == 3 and not any(z.mitigated for z in e.bullFvgList)
