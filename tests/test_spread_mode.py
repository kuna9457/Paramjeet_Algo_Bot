"""Option hedge = sell a PUT (BUY signal) / CALL (SELL signal) inside its premium range, hedge with a further-OTM
option of the same type inside its own range. Settings, migration of old settings files, end to end, signal log."""
from datetime import datetime
from pathlib import Path

import pytest

import bot.live.runner as runner_mod
from bot.config import Section, load_settings
from bot.data.candle import IST
from bot.hedge.scanner import range_text
from bot.strategy.engine import Event
from bot.ui import settings_io, signal_view
from tests.test_runner import make_bot

BASELINE = Path(__file__).parent / "data" / "settings_baseline.yaml"


def test_range_text():
    cfg = Section({"sell_min_premium": 0, "sell_max_premium": 55, "hedge_diff_min": 10, "hedge_diff_max": 15,
                   "sell_strike_rule": "ends_with_50", "buy_strike_rule": "ends_with_50"})
    assert range_text(cfg) == "sell premium <= 55 (ends_with_50), sell - hedge premium 10-15 (ends_with_50)"


# ------------------------------------------------------------------ settings
def test_baseline_settings_and_validation(tmp_path):
    h = load_settings(BASELINE).hedge
    assert (h.sell_min_premium, h.sell_max_premium, h.hedge_diff_min, h.hedge_diff_max) == (0, 55, 10, 15)
    assert "buy_min_premium" not in h.__dict__
    assert h.buy_strike_rule == h.sell_strike_rule == "ends_with_50"
    assert "pair_mode" not in h.__dict__ and "range_min" not in h.__dict__
    p = tmp_path / "settings.yaml"
    p.write_text(BASELINE.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError):
        settings_io.save({("hedge", "hedge_diff_min"): 20, ("hedge", "hedge_diff_max"): 10}, p)
    with pytest.raises(ValueError):
        settings_io.save({("hedge", "sell_max_premium"): -1}, p)
    with pytest.raises(ValueError):
        settings_io.save({("hedge", "sell_strike_rule"): "ends_with_25"}, p)
    settings_io.save({("hedge", "sell_max_premium"): 60}, p)
    assert load_settings(p).hedge.sell_max_premium == 60


OLD_HEDGE = """hedge:
  expiry_mode: nearest_weekly
  pair_mode: credit            # debit | credit
  range_min: 20               # DEBIT mode
  range_max: 30
  credit_min: 10              # CREDIT mode
  credit_max: 15
  sell_max_premium: 50        # CREDIT mode: sell leg premium must be <= this (0 = no cap)
  buy_min_premium: 35         # old hedge premium band
  buy_max_premium: 40
  strike_step: 50
"""


def test_old_settings_file_is_migrated(tmp_path):
    base = BASELINE.read_text(encoding="utf-8")
    start = base.index("hedge:")
    end = base.index("\n\n", start)
    p = tmp_path / "settings.yaml"
    p.write_text(base[:start] + OLD_HEDGE + base[end + 1:], encoding="utf-8")
    assert settings_io.ensure_new_keys(p) is True
    text = p.read_text(encoding="utf-8")
    for k in ("pair_mode:", "range_min:", "range_max:", "credit_min:", "credit_max:", "buy_min_premium:", "buy_max_premium:"):
        assert k not in text
    h = load_settings(p).hedge
    assert h.sell_max_premium == 50                                    # saved value kept
    assert (h.sell_min_premium, h.hedge_diff_min, h.hedge_diff_max) == (0, 10, 15)   # new keys added
    assert settings_io.ensure_new_keys(p) is False


# ------------------------------------------------------------------ end to end with the fake broker
def _bot(tmp_path, monkeypatch, **hedge):
    bot, fake = make_bot(tmp_path)
    for k, v in hedge.items():
        setattr(bot.S.hedge, k, v)
    monkeypatch.setattr(runner_mod, "now_ist", lambda: datetime(2026, 11, 9, 10, 0, tzinfo=IST))
    from bot.strategy.engine import NexusEngine
    bot.engine = NexusEngine(bot.S.strategy)
    return bot, fake


EV = Event("ENTRY", 100, "2026-11-09T10:00", 1, "IDM", 23480.0, 23400.0, 23640.0, 23560.0, 2.0)


def test_trade_end_to_end_and_signal_log(tmp_path, monkeypatch):
    bot, _ = _bot(tmp_path, monkeypatch, hedge_diff_min=12, hedge_diff_max=18)
    bot.handle_events([EV])
    c = bot.open_combos()[0]
    assert "pair_mode" not in c
    assert c["sell"]["type"] == c["buy"]["type"] == "PE" and c["buy"]["strike"] < c["sell"]["strike"]
    assert c["sell"]["scan_ltp"] <= 55 and c["scan_net"] > 0
    act = bot.storage.find("signal_actions")[0]
    assert act["outcome"] == "TRADED" and act["combo_id"] == c["id"]
    assert act["top_candidates"] and act["wanted"].startswith("sell premium <= 55")
    assert 12 <= c["scan_net"] <= 18                                  # sell - hedge premium inside the range


def test_skipped_signals_are_logged_with_reason(tmp_path, monkeypatch):
    bot, _ = _bot(tmp_path, monkeypatch, sell_max_premium=1)            # nothing that cheap in the chain
    bot.handle_events([EV])
    assert not bot.open_combos()
    act = bot.storage.find("signal_actions")[0]
    assert act["outcome"] == "SKIPPED" and "No option pair matched" in act["reason"]

    bot2, _ = _bot(tmp_path / "k", monkeypatch)
    bot2.S.safety.kill_switch = True
    bot2.handle_events([EV])
    a2 = bot2.storage.find("signal_actions")[0]
    assert a2["outcome"] == "SKIPPED" and a2["reason"] == "Kill switch active"


# ------------------------------------------------------------------ dashboard join
def test_signal_rows_join_events_actions_and_combos(tmp_path, monkeypatch):
    bot, _ = _bot(tmp_path, monkeypatch)
    bot.handle_events([EV])
    combo = bot.open_combos()[0]
    bot._close(combo, "STRATEGY_EXIT")
    rows = signal_view.signal_rows(bot.storage.find("events"), bot.storage.find("combos"),
                                   bot.storage.find("signal_actions"))
    assert len(rows) == 1
    r = rows[0]
    assert r["signal"] == "BUY" and r["action"] == "TRADED" and r["BUY leg"].endswith("PE") and r["SELL leg"].endswith("PE")
    assert r["net premium"].startswith("received") and r["exit reason"] == "STRATEGY_EXIT" and r["P&L ₹"] is not None
    assert "pair mode" not in r
    legs = signal_view.leg_rows(bot.storage.find("combos")[0])
    assert {x["broker"] for x in legs} == {"paper-upstox", "paper-zerodha"}
    assert all(x["BUY entry"] and x["SELL entry"] and x["P&L ₹"] is not None for x in legs)


def test_signal_rows_work_for_old_data_without_decision_log():
    ev = {"kind": "ENTRY", "time": "2026-11-09T10:00", "direction": -1, "trigger": "CHoCH", "price": 23480.0,
          "sl": 23560.0, "target": 23320.0, "rr": 2.0, "mode": "paper"}
    combo = {"id": "x", "signal_time": "2026-11-09T10:00", "direction": -1, "trigger": "CHoCH", "status": "CLOSED",
             "expiry": "2026-11-10", "pair_mode": "debit", "buy": {"strike": 23500.0, "type": "PE", "scan_ltp": 130.0},
             "sell": {"strike": 23350.0, "type": "CE", "scan_ltp": 100.0}, "scan_diff": 30.0,
             "legs": {"paper-upstox": {"buy": {"entry": 131.0, "qty": 75}, "sell": {"entry": 99.0, "qty": 75}, "pnl": 500.0}},
             "pnl": {"paper-upstox": 500.0}, "exit_reason": "TARGET"}
    r = signal_view.signal_rows([ev], [combo], [])[0]
    assert r["action"] == "TRADED" and r["net premium"] == "paid 32.00" and r["P&L ₹"] == 500.0
    nothing = signal_view.signal_rows([ev], [], [])[0]
    assert nothing["action"].startswith("NOT TRADED")
