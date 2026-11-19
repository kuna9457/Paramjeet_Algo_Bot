"""v8.7 = v8.0 + HTML-backtest sync changes. v7.4 and v8.0 must stay byte-identical."""
import json
from dataclasses import replace

import pytest

from bot.config import StrategySettings, load_preset
from bot.strategy.engine import NexusEngine
from tests.baseline_v74 import GOLDEN, GOLDEN_V80, snapshot

V87_GOLDEN = GOLDEN.with_name("v87_golden.json")


def eng(version="v8.7", **kw):
    return NexusEngine(replace(StrategySettings(**load_preset("v8.7")), strategy_version=version, **kw))


def open_trade(e, d=1, entry=23500.0, sl_pts=70.0, target_pts=105.0, bar=100, rr=1.5):
    e.bar_index = bar
    e.activeTradeDir = d
    e.entryPrice = entry
    e.slPrice = entry - d * sl_pts
    e.targetPrice = entry + d * target_pts
    e.initialRiskPoints = sl_pts
    e.currentTradeRr = rr
    e.tp1Price = entry + d * sl_pts
    e.tp1Hit = False
    e.tp1HitBarIndex = -100
    e.lastSignalBarIndex = bar


def step(e, d, o, h, l, c, held=1):
    e.bar_index = e.lastSignalBarIndex + held
    ev = []
    e._manage(d, False, o, h, l, c, 70.0, "t", ev)
    return [x for x in ev if x.kind == "EXIT"]


# ------------------------------------------------------------------ parity of older versions
def test_v74_and_v80_unchanged_by_v87():
    assert snapshot() == json.loads(GOLDEN.read_text())
    assert snapshot("v8.0") == json.loads(GOLDEN_V80.read_text())


def test_v87_output_is_pinned_and_differs_from_v80():
    now = snapshot("v8.7")
    assert now == json.loads(V87_GOLDEN.read_text())
    v80 = json.loads(GOLDEN_V80.read_text())
    assert any(now[k] != v80[k] for k in now)


def test_version_flags():
    assert (eng("v7.4").v8, eng("v7.4").v87) == (False, False)
    assert (eng("v8.0").v8, eng("v8.0").v87) == (True, False)
    assert (eng("v8.7").v8, eng("v8.7").v87) == (True, True)


# ------------------------------------------------------------------ quant exits (long)
def test_long_exits_at_plus_70_before_the_planned_target():
    e = eng(); open_trade(e)
    (x,) = step(e, 1, 23510, 23575, 23495, 23560)
    assert x.reason == "QUANT_70" and x.price == 23570.0 and x.pnl_points == pytest.approx(70 - 3.0)
    assert e.winTradesCount == 1 and e.activeTradeDir == 0
    assert e.accumulatedNetR == pytest.approx(max(1.0, 1.5) - 3.0 / 70.0)


def test_plus_70_and_stop_in_same_bar_counts_as_loss():
    e = eng(); open_trade(e)
    (x,) = step(e, 1, 23500, 23575, 23425, 23550)
    assert x.reason == "SL" and e.lossTradesCount == 1 and e.winTradesCount == 0


def test_planned_target_still_reports_target():
    e = eng(); open_trade(e, target_pts=60.0)               # target nearer than +70
    (x,) = step(e, 1, 23510, 23565, 23500, 23560)
    assert x.reason == "TARGET" and x.price == 23560.0


def test_liquidity_decay_win_needs_12_bars_and_45_pts():
    e = eng(); open_trade(e)
    assert step(e, 1, 23540, 23560, 23535, 23547, held=11) == []          # only 11 bars held
    assert e.activeTradeDir == 1
    assert step(e, 1, 23540, 23560, 23535, 23544, held=12) == []          # +44 < +45
    (x,) = step(e, 1, 23540, 23560, 23535, 23545, held=12)
    assert x.reason == "DECAY_12" and x.price == 23545.0 and x.pnl_points == pytest.approx(45 - 3.0)


def test_plus_70_arms_breakeven_even_when_tp1_is_further():
    e = eng(); open_trade(e, sl_pts=90.0, target_pts=135.0)   # TP1 sits at +90
    ev = []
    e.bar_index = 101
    e._manage(1, False, 23500, 23560, 23450, 23520, 90.0, "t", ev)        # high < +70: nothing yet
    assert not e.tp1Hit and e.activeTradeDir == 1
    e.bar_index = 102
    e._manage(1, False, 23520, 23572, 23510, 23560, 90.0, "t", ev)        # high >= +70 -> BE armed (and win booked)
    assert e.tp1Hit


# ------------------------------------------------------------------ quant exits (short mirror)
def test_short_mirror_of_quant_rules():
    e = eng(); open_trade(e, d=-1)
    (x,) = step(e, -1, 23490, 23505, 23425, 23440)
    assert x.reason == "QUANT_70" and x.price == 23430.0 and x.pnl_points == pytest.approx(70 - 3.0)
    e = eng(); open_trade(e, d=-1)
    (x,) = step(e, -1, 23460, 23465, 23450, 23455, held=12)
    assert x.reason == "DECAY_12" and x.price == 23455.0


def test_older_versions_do_not_use_quant_exits():
    for v in ("v7.4", "v8.0"):
        e = eng(v); open_trade(e)
        assert step(e, 1, 23510, 23575, 23495, 23560) == []                   # +70 touched, target 105 not
        assert step(e, 1, 23540, 23560, 23535, 23547, held=12) == []          # no 12-bar decay rule


def test_effective_tick_target():
    e = eng(); open_trade(e)
    assert e.effective_target() == 23570.0                                    # nearer of target(105) and +70
    e = eng(); open_trade(e, target_pts=60.0)
    assert e.effective_target() == 23560.0
    e = eng(); open_trade(e, d=-1)
    assert e.effective_target() == 23430.0
    e = eng("v8.0"); open_trade(e)
    assert e.effective_target() == 23605.0                                    # unchanged for older versions


# ------------------------------------------------------------------ target planning
def test_fallback_target_is_70_points_and_respects_min_rr():
    e = eng()
    e.pivotHighLevels, e.pivotLowLevels = [], []
    entry, sl, sl_dist, target, rr = e._plan_trade(1, 23500.0, 40.0, False, 23400.0, 23600.0)
    assert (sl_dist, target, rr) == (70.0, 23500.0 + 105.0, 1.5)             # 70/70 = 1R < 1.5R -> extended (setting)
    e2 = eng(minTargetRr=1.0)
    e2.pivotHighLevels, e2.pivotLowLevels = [], []
    assert e2._plan_trade(1, 23500.0, 40.0, False, 0, 0)[3] == 23570.0       # raw 70-pt target
    assert e2._plan_trade(-1, 23500.0, 40.0, False, 0, 0)[3] == 23430.0


def test_swing_target_beyond_90_points_falls_back_to_70():
    e = eng(strat15_LiquidityRun=True, minTargetRr=1.0)
    e.pivotHighLevels = [23650.0, 23585.0]
    e.pivotLowLevels = [23300.0]
    assert e._plan_trade(1, 23500.0, 40.0, False, 0, 0)[3] == 23585.0        # 85 away: kept
    e.pivotHighLevels = [23650.0]
    assert e._plan_trade(1, 23500.0, 40.0, False, 0, 0)[3] == 23570.0        # 150 away: -> 70
    e.pivotLowLevels = [23290.0]
    assert e._plan_trade(-1, 23500.0, 40.0, False, 0, 0)[3] == 23430.0


def test_v80_fallback_target_is_2r():
    e = eng("v8.0", minTargetRr=1.0)
    e.pivotHighLevels, e.pivotLowLevels = [], []
    assert e._plan_trade(1, 23500.0, 40.0, False, 0, 0)[3] == 23640.0


# ------------------------------------------------------------------ presets / settings
def test_v87_preset_is_v80_inputs_with_cross_trigger_off():
    a, b = load_preset("v8.0"), load_preset("v8.7")
    assert {k for k in a if a[k] != b[k]} == {"strategy_version", "allowMultiTriggerEntries"}
    assert b["strategy_version"] == "v8.7" and b["allowMultiTriggerEntries"] is False
    assert b["strictTriggerLock"] is True


def test_v87_accepted_by_settings_and_rejects_unknown():
    from bot.config import _build_strategy
    assert _build_strategy({"strategy_version": "v8.7"}, {}).strategy_version == "v8.7"
    with pytest.raises(ValueError):
        _build_strategy({"strategy_version": "v8.8"}, {})


def test_new_exit_reasons_only_appear_in_v87():
    from bot.data.candle import resample
    from tests.fakes import synthetic_1m
    bars = resample(synthetic_1m(days=60), 5)
    reasons = {}
    for v in ("v8.0", "v8.7"):
        e = eng(v, strat3_OB=True, strat4_FVG=True)
        for b in bars:
            for x in e.on_bar(b):
                if x.kind == "EXIT":
                    reasons.setdefault(v, set()).add(x.reason)
    assert not ({"QUANT_70", "DECAY_12"} & reasons.get("v8.0", set()))
    assert {"QUANT_70", "DECAY_12", "TARGET", "SL"} & reasons["v8.7"]


# ------------------------------------------------------------------ trigger priority
def test_trigger_priority_v87_zone_triggers_first():
    from bot.strategy.engine import trigger_name
    # args: t3 (CHoCH), t4 (IDM), t1 (OB), t2 (FVG)
    assert trigger_name(True, True, True, False, v87=False) == "CHoCH+IDM"
    assert trigger_name(True, True, True, False, v87=True) == "OB"
    assert trigger_name(True, False, False, True, v87=False) == "CHoCH"
    assert trigger_name(True, False, False, True, v87=True) == "FVG"
    assert trigger_name(True, True, False, False, v87=True) == "CHoCH+IDM"
    assert trigger_name(False, True, False, False, v87=True) == "IDM"
    assert trigger_name(False, False, False, False, v87=True) == ""
    assert trigger_name(False, False, True, True, v87=False) == "OB"


# ------------------------------------------------------------------ FVG mitigation timing
def _warm(version):
    from bot.data.candle import resample
    from tests.fakes import synthetic_1m
    e = eng(version)
    bars = resample(synthetic_1m(days=8), 5)
    for b in bars:
        e.on_bar(b)
    return e, bars[-1]


def _fvg_after(version, *, low, close, touch_age):
    from datetime import timedelta
    from bot.data.candle import Candle
    from bot.strategy.engine import Zone
    e, last = _warm(version)
    bi = e.bar_index + 1
    z = Zone(23500.0, 23450.0, True, birthBar=0, touchBar=(bi - touch_age) if touch_age else 0)
    e.bullFvgList, e.bearFvgList, e.bullObList, e.bearObList = [z], [], [], []
    e.on_bar(Candle(last.time + timedelta(minutes=5), 23480.0, 23490.0, low, close, 1000))
    return z.mitigated


def test_fvg_touching_the_bottom_exactly_no_longer_mitigates_in_v87():
    # low == zone bottom, close back inside the gap
    assert _fvg_after("v8.0", low=23450.0, close=23470.0, touch_age=0) is True
    assert _fvg_after("v8.7", low=23450.0, close=23470.0, touch_age=0) is False
    assert _fvg_after("v8.7", low=23449.0, close=23470.0, touch_age=0) is True     # a real break below still does


def test_fvg_reaction_window_is_longer_in_v87():
    for age, v80, v87 in ((3, True, False), (4, True, False), (5, True, True)):
        assert _fvg_after("v8.0", low=23470.0, close=23480.0, touch_age=age) is v80, age
        assert _fvg_after("v8.7", low=23470.0, close=23480.0, touch_age=age) is v87, age
