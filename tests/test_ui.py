import shutil

import yaml

from backtest.runner import run_backtest
from bot.config import DEFAULT_SETTINGS, load_settings
from bot.ui import charts, settings_io
from tests.fakes import synthetic_1m


def test_settings_roundtrip_keeps_comments(tmp_path):
    p = tmp_path / "settings.yaml"
    shutil.copyfile(DEFAULT_SETTINGS, p)
    before = p.read_text(encoding="utf-8")
    upd = {("hedge", "range_min"): 25, ("hedge", "range_max"): 40, ("brokers", "lots", "zerodha"): 3,
           ("brokers", "enabled"): ["upstox"], ("strategy", "slMode"): "Custom Points",
           ("strategy", "obTimeframe"): "15", ("strategy", "strat3_OB"): True, ("hedge", "expiry_exit_time"): "15:10"}
    settings_io.save(upd, p)
    after = p.read_text(encoding="utf-8")
    raw = yaml.safe_load(after)
    assert raw["hedge"]["range_min"] == 25 and raw["hedge"]["range_max"] == 40
    assert raw["brokers"]["lots"] == {"upstox": 1, "zerodha": 3}
    assert raw["brokers"]["enabled"] == ["upstox"]
    assert raw["strategy"]["slMode"] == "Custom Points" and raw["strategy"]["obTimeframe"] == "15"
    assert raw["hedge"]["expiry_exit_time"] == "15:10"
    assert before.count("#") == after.count("#")          # comments preserved
    assert len(before.splitlines()) == len(after.splitlines())
    s = load_settings(p)
    assert s.strategy.strat3_OB is True


def test_settings_invalid_is_rolled_back(tmp_path):
    p = tmp_path / "settings.yaml"
    shutil.copyfile(DEFAULT_SETTINGS, p)
    good = p.read_text(encoding="utf-8")
    try:
        settings_io.save({("bot", "chart_timeframe"): "abc"}, p)
        assert False, "should fail"
    except Exception:
        pass
    assert p.read_text(encoding="utf-8") == good


def test_comments_and_groups_scan():
    _, text = settings_io.read_raw()
    comments, groups = settings_io.scan(text)
    assert "premium difference" in comments[("hedge", "range_min")]
    assert groups[("strategy", "strat1_IDM")].startswith("1. IDM")


def test_backtest_chart_builds():
    s = load_settings()
    res = run_backtest(synthetic_1m(30), s.strategy)
    bars = [{"t": c.time.isoformat(), "o": c.open, "h": c.high, "l": c.low, "c": c.close,
             "sh": d.get("protectedStrongHigh"), "sl": d.get("protectedStrongLow"), "eq": d.get("equilibrium")}
            for c, d in zip(res["feed"], res["diag"])]
    df = charts.bars_frame(bars)
    trades = charts.trade_dicts(res["trades"])
    fig = charts.price_chart(df, trades, 5)
    assert len(fig.data) >= 2
    if trades:
        a, b = charts.window_for(trades[0], 5)
        charts.price_chart(df, trades, 5, xrange=(a, b))
        charts.equity_chart(trades)


def test_add_missing_keys_restores_new_settings_without_touching_saved_values():
    import yaml
    from bot.config import DEFAULT_SETTINGS
    from bot.ui.settings_io import add_missing_keys
    default = DEFAULT_SETTINGS.read_text(encoding="utf-8")
    old = "\n".join(l for l in default.splitlines() if "fvgTimeframe" not in l).replace("maxFvgCount: 5", "maxFvgCount: 9")
    merged = add_missing_keys(old, default)
    d = yaml.safe_load(merged)
    assert d["strategy"]["fvgTimeframe"] == "" and d["strategy"]["maxFvgCount"] == 9
    assert merged.index("fvgTimeframe") < merged.index("maxFvgCount")
    assert add_missing_keys(merged, default) == merged
