from dataclasses import replace

from bot.config import StrategySettings
from bot.data.candle import resample
from bot.strategy.engine import NexusEngine
from tests.fakes import synthetic_1m

M1 = synthetic_1m(days=60)
M5 = resample(M1, 5)


def run(settings):
    e = NexusEngine(settings)
    events = []
    for b in M5:
        events += e.on_bar(b)
    return e, events


def test_default_settings_produce_consistent_trades():
    e, events = run(StrategySettings())
    entries = [x for x in events if x.kind == "ENTRY"]
    exits = [x for x in events if x.kind == "EXIT"]
    assert entries, "expected at least one signal on 60 synthetic days"
    assert len(exits) in (len(entries), len(entries) - 1)
    for t in e.trades:
        d = t.direction
        assert (t.entry - t.sl) * d == 80.0                       # Fixed 80 pts preset
        assert round((t.target - t.entry) * d / 80.0, 6) == 2.0    # default target 2R
        assert t.reason in ("SL", "BE", "TARGET", "TIME", "CHOCH")
    st = e.stats()
    assert st["wins"] + st["losses"] + st["breakeven"] >= len(e.trades) - st["time_exits"] - 1


def test_deterministic():
    a, ea = run(StrategySettings())
    b, eb = run(StrategySettings())
    assert [x.to_dict() for x in ea] == [x.to_dict() for x in eb]


def test_all_features_enabled_runs():
    s = replace(StrategySettings(), strat3_OB=True, strat4_FVG=True, slMode="Swing High/Low",
                strat15_LiquidityRun=True, enableMaxTargetCap=True, allowMultiTriggerEntries=True,
                strat16_POI_Confluence=False, enableAdxFilter=True, enableChopFilter=True, filterEmaSqueeze=True,
                strat10_Momentum_CISD=True, strat8_HTF_IDM_Trap=True, fix_ob_mitigation=True)
    e, events = run(s)
    for t in e.trades:
        assert 0 < abs(t.entry - t.sl) <= s.maxAllowedSlPoints
        assert t.rr <= s.maxTargetRr + 1e-9


def test_chart_timeframe_pivots_path():
    s = replace(StrategySettings(), chochStructureTf="", idmTf="", htfTimeframe="")
    e, _ = run(s)
    assert not e.useHtfStructure and not e.useHtfIdm


def test_triggers_off_means_silence():
    s = replace(StrategySettings(), strat1_IDM=False, strat2_CHOCH=False, strat3_OB=False, strat4_FVG=False)
    _, events = run(s)
    assert not [x for x in events if x.kind == "ENTRY"]


def test_other_timeframe_15m():
    s = replace(StrategySettings(), chart_timeframe=15)
    e = NexusEngine(s)
    for b in resample(M1, 15):
        e.on_bar(b)
    assert e.bar_index > 1000
