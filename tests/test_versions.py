"""v7.4 must stay byte-identical; v8.0 adds the per-trigger lock and other Pine v8.0 changes."""
import json
from dataclasses import asdict, fields, replace
from pathlib import Path

import pytest
import yaml

from bot.config import STRATEGY_VERSIONS, StrategySettings, _build_strategy, load_preset
from bot.data.candle import resample
from bot.strategy.engine import NexusEngine
from tests.baseline_v74 import GOLDEN, snapshot
from tests.fakes import synthetic_1m

M5 = resample(synthetic_1m(days=60), 5)


def run(s):
    e = NexusEngine(s)
    ev = []
    for b in M5:
        ev += e.on_bar(b)
    return e, ev


def v8_settings(**kw):
    return replace(StrategySettings(**load_preset("v8.0")), **kw)


# ------------------------------------------------------------------ v7.4 parity
def test_v74_output_identical_to_golden_snapshot():
    assert snapshot() == json.loads(GOLDEN.read_text())


def test_strict_lock_setting_is_ignored_in_v74():
    _, a = run(replace(StrategySettings(), allowMultiTriggerEntries=True, strictTriggerLock=True))
    _, b = run(replace(StrategySettings(), allowMultiTriggerEntries=True, strictTriggerLock=False))
    assert [x.to_dict() for x in a] == [x.to_dict() for x in b]


def test_default_version_is_v74():
    assert StrategySettings().strategy_version == "v7.4"
    assert NexusEngine(StrategySettings()).v8 is False


# ------------------------------------------------------------------ presets
def test_presets_cover_every_strategy_setting():
    names = {f.name for f in fields(StrategySettings)} - {"chart_timeframe", "mintick"}
    for v in STRATEGY_VERSIONS:
        p = load_preset(v)
        assert set(p) == names
        assert p["strategy_version"] == v


def test_v74_preset_equals_original_defaults():
    d = asdict(StrategySettings())
    assert all(d[k] == v for k, v in load_preset("v7.4").items())


def test_v80_preset_matches_pine_v80_inputs():
    p = load_preset("v8.0")
    assert (p["idmTf"], p["idmLookback"], p["strat8_HTF_IDM_Trap"]) == ("5", 4, True)
    assert (p["chochStructureTf"], p["swingLength"]) == ("15", 20)
    assert (p["strat3_OB"], p["obTimeframe"], p["maxOrderBlocks"]) == (True, "15", 25)
    assert (p["strat4_FVG"], p["fvgTimeframe"], p["fvgAtrMultiplier"], p["maxFvgCount"]) == (True, "15", 0.65, 25)
    assert (p["strat7_PD_Filter"], p["strat13_Psychology1"], p["enableAdxFilter"]) == (False, False, True)
    assert (p["strictTriggerLock"], p["allowMultiTriggerEntries"]) == (True, True)
    assert (p["slFixedPreset"], p["strat15_LiquidityRun"]) == ("70 Points", True)


def test_invalid_version_rejected():
    with pytest.raises(ValueError):
        _build_strategy({"strategy_version": "v9"}, {})


# ------------------------------------------------------------------ v8.0 logic
def test_v80_runs_and_trades():
    e, ev = run(v8_settings())
    entries = [x for x in ev if x.kind == "ENTRY"]
    assert e.v8 and entries
    for t in e.trades:
        assert t.reason in ("SL", "BE", "TARGET", "TIME", "CHOCH", "REPLACED")
        assert round(abs(t.entry - t.sl), 6) == 70.0


def test_v80_differs_from_v74_with_same_inputs():
    s = v8_settings()
    _, a = run(s)
    _, b = run(replace(s, strategy_version="v7.4"))
    assert [x.to_dict() for x in a] != [x.to_dict() for x in b]


def test_v80_never_opens_same_trigger_while_its_lock_is_running():
    e = NexusEngine(v8_settings())
    for b in M5:
        before = {k: list(v) for k, v in e.trigLock.items()}
        evs = e.on_bar(b)
        for x in (x for x in evs if x.kind == "ENTRY"):
            for name, (d, sl, tgt, start) in before.items():
                if name not in x.trigger or d == 0:
                    continue
                # the lock must have been released on this bar by its own SL/target touch or an opposite CHoCH
                hit = (b.low <= sl or b.open <= sl or b.high >= tgt or b.open >= tgt) if d == 1 else \
                      (b.high >= sl or b.open >= sl or b.low <= tgt or b.open <= tgt)
                choch = e.diag["isBearishChoch"] if d == 1 else e.diag["isBullishChoch"]
                assert hit or choch, (name, x.to_dict())


def test_trigger_lock_helpers():
    e = NexusEngine(v8_settings())
    e.bar_index = 10
    e._set_trigger_locks("CHoCH+IDM", 1, 100.0, 200.0)
    assert e.trigger_locks() == {"FVG": 0, "IDM": 1, "OB": 0, "CHoCH": 1}
    assert not e._trigger_free("IDM") and not e._trigger_free("CHoCH") and not e._trigger_free("CHoCH+IDM")
    assert e._trigger_free("FVG") and e._trigger_free("OB")
    e._update_trigger_locks(150, 160, 140, False, False)      # same bar as entry -> untouched
    assert e.trigger_locks()["IDM"] == 1
    e.bar_index = 11
    e._update_trigger_locks(150, 160, 140, False, False)      # inside SL/target -> still locked
    assert e.trigger_locks()["IDM"] == 1
    e._update_trigger_locks(150, 201, 140, False, False)      # target touched -> released
    assert e.trigger_locks() == {"FVG": 0, "IDM": 0, "OB": 0, "CHoCH": 0}
    e._set_trigger_locks("FVG", -1, 200.0, 100.0)
    e.bar_index = 12
    e._update_trigger_locks(150, 160, 140, True, False)       # opposite (bullish) CHoCH releases a short
    assert e.trigger_locks()["FVG"] == 0


def test_strict_lock_off_in_v80_changes_nothing_else():
    e, _ = run(v8_settings(strictTriggerLock=False))
    assert e.v8


# ------------------------------------------------------------------ old settings.yaml upgrade
def test_ensure_new_keys_upgrades_old_settings_file(tmp_path: Path):
    from bot.config import load_settings
    DEFAULT_SETTINGS = Path(__file__).parent / "data" / "settings_baseline.yaml"
    from bot.ui import settings_io
    old = "".join(ln for ln in DEFAULT_SETTINGS.read_text(encoding="utf-8").splitlines(keepends=True)
                  if "strategy_version:" not in ln and "strictTriggerLock:" not in ln)
    p = tmp_path / "settings.yaml"
    p.write_text(old, encoding="utf-8")
    assert settings_io.ensure_new_keys(p) is True
    s = load_settings(p).strategy
    assert s.strategy_version == "v7.4" and s.strictTriggerLock is True
    assert settings_io.ensure_new_keys(p) is False
    before = yaml.safe_load(old)["strategy"]
    after = yaml.safe_load(p.read_text(encoding="utf-8"))["strategy"]
    assert all(after[k] == v for k, v in before.items())
