"""Credit pair mode (sell the richer leg, buy the cheaper one, collect a net credit) + per-signal decision log."""
from datetime import datetime

import pytest

import bot.live.runner as runner_mod
from bot.brokers.base import Instrument, OptionQuote
from bot.config import Section, load_settings
from bot.data.candle import IST
from bot.hedge.scanner import find_pair, range_text
from bot.strategy.engine import Event
from bot.ui import signal_view
from tests.test_hedge import q
from tests.test_runner import make_bot

CREDIT = Section({"pair_mode": "credit", "credit_min": 10, "credit_max": 15, "sell_max_premium": 55,
                  "buy_min_premium": 35, "buy_max_premium": 40, "range_min": 20, "range_max": 30,
                  "strike_step": 50, "strike_scan_width": 10, "buy_strike_rule": "any",
                  "sell_strike_rule": "ends_with_50", "min_leg_premium": 5})


# ------------------------------------------------------------------ scanner
def test_client_example_sell_put_55_buy_call_40_is_a_credit_pair():
    chain = [q(23600, "CE", 40), q(23450, "PE", 55)]
    best, matches = find_pair(1, chain, 23480, CREDIT)
    assert best is not None and len(matches) == 1
    assert (best.buy.opt_type, best.buy.strike, best.sell.opt_type, best.sell.strike) == ("CE", 23600, "PE", 23450)
    assert best.mode == "credit" and best.diff == 15 and best.net == 15
    assert "net CREDIT 15.00" in best.describe()


def test_credit_limits_are_enforced():
    base = [q(23600, "CE", 38)]
    assert find_pair(1, base + [q(23450, "PE", 52)], 23480, CREDIT)[0] is not None      # credit 14, sell<=55, buy 35-40
    assert find_pair(1, base + [q(23450, "PE", 56)], 23480, CREDIT)[0] is None          # sell premium above the 55 cap
    assert find_pair(1, base + [q(23450, "PE", 60)], 23480, CREDIT)[0] is None          # credit 22 and above cap
    assert find_pair(1, base + [q(23450, "PE", 46)], 23480, CREDIT)[0] is None          # credit 8 < min 10
    assert find_pair(1, [q(23600, "CE", 41), q(23450, "PE", 55)], 23480, CREDIT)[0] is None   # buy leg above 40
    assert find_pair(1, [q(23600, "CE", 34), q(23450, "PE", 46)], 23480, CREDIT)[0] is None   # buy leg below 35
    assert find_pair(1, base + [q(23400, "PE", 50)], 23480, CREDIT)[0] is None          # 00 strike never sold


def test_credit_mode_rejects_debit_pairs_and_debit_mode_rejects_credit_pairs():
    debit_pair = [q(23500, "CE", 130), q(23350, "PE", 100)]                       # buy dearer than sell
    assert find_pair(1, debit_pair, 23480, CREDIT)[0] is None
    credit_pair = [q(23600, "CE", 40), q(23450, "PE", 55)]
    debit_cfg = Section({**CREDIT.__dict__, "pair_mode": "debit"})
    assert find_pair(1, credit_pair, 23480, debit_cfg)[0] is None


def test_credit_mode_sell_signal_mapping_and_zero_means_unlimited():
    cfg = Section({**CREDIT.__dict__, "sell_max_premium": 0, "buy_min_premium": 0, "buy_max_premium": 0})
    chain = [q(23400, "PE", 90), q(23550, "CE", 100)]                             # SELL signal: buy PE, sell CE
    best, _ = find_pair(-1, chain, 23480, cfg)
    assert best.buy.opt_type == "PE" and best.sell.opt_type == "CE" and best.sell.strike == 23550 and best.diff == 10


def test_default_mode_is_debit_and_unchanged():
    cfg = Section({k: v for k, v in CREDIT.__dict__.items() if k != "pair_mode"})
    best, _ = find_pair(1, [q(23500, "CE", 130), q(23350, "PE", 100)], 23480, cfg)
    assert best.mode == "debit" and best.diff == 30 and best.net == -30


def test_range_text():
    assert range_text(CREDIT) == "net credit 10-15 (sell premium <= 55, buy premium 35-40)"
    assert range_text(Section({"range_min": 20, "range_max": 30})) == "net debit 20-30"


# ------------------------------------------------------------------ settings validation
def test_settings_yaml_defaults_to_debit_and_validates(tmp_path):
    from bot.ui import settings_io
    s = load_settings()
    assert s.hedge.pair_mode == "debit"
    p = tmp_path / "settings.yaml"
    p.write_text(open("config/settings.yaml", encoding="utf-8").read(), encoding="utf-8")
    with pytest.raises(ValueError):
        settings_io.save({("hedge", "pair_mode"): "sideways"}, p)
    with pytest.raises(ValueError):
        settings_io.save({("hedge", "pair_mode"): "credit", ("hedge", "credit_min"): 20, ("hedge", "credit_max"): 10}, p)
    settings_io.save({("hedge", "pair_mode"): "credit"}, p)
    assert load_settings(p).hedge.pair_mode == "credit"


def test_old_settings_file_gets_hedge_keys(tmp_path):
    from bot.ui import settings_io
    old = "".join(ln for ln in open("config/settings.yaml", encoding="utf-8").read().splitlines(keepends=True)
                  if not any(k in ln for k in ("pair_mode:", "credit_min:", "credit_max:", "sell_max_premium:",
                                               "buy_min_premium:", "buy_max_premium:")))
    p = tmp_path / "settings.yaml"
    p.write_text(old, encoding="utf-8")
    assert settings_io.ensure_new_keys(p) is True
    h = load_settings(p).hedge
    assert h.pair_mode == "debit" and (h.credit_min, h.credit_max, h.sell_max_premium) == (10, 15, 55)
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


def test_credit_trade_end_to_end_and_signal_log(tmp_path, monkeypatch):
    bot, _ = _bot(tmp_path, monkeypatch, pair_mode="credit", credit_min=1, credit_max=300, sell_max_premium=0,
                  buy_min_premium=0, buy_max_premium=0)
    bot.handle_events([EV])
    c = bot.open_combos()[0]
    assert c["pair_mode"] == "credit" and c["scan_net"] > 0
    assert c["sell"]["scan_ltp"] > c["buy"]["scan_ltp"]
    for leg in c["legs"].values():
        assert leg["net_entry"] > 0 and leg["net_entry_rs"] > 0           # collected a credit on every broker
    act = bot.storage.find("signal_actions")[0]
    assert act["outcome"] == "TRADED" and act["combo_id"] == c["id"] and act["pair_mode"] == "credit"
    assert act["top_candidates"] and act["wanted"].startswith("net credit")


def test_debit_default_trade_unchanged_and_logged(tmp_path, monkeypatch):
    bot, _ = _bot(tmp_path, monkeypatch)            # settings.yaml default = debit
    bot.handle_events([EV])
    c = bot.open_combos()[0]
    assert c["pair_mode"] == "debit" and c["scan_net"] < 0
    assert c["buy"]["scan_ltp"] > c["sell"]["scan_ltp"]


def test_skipped_signals_are_logged_with_reason(tmp_path, monkeypatch):
    bot, _ = _bot(tmp_path, monkeypatch, pair_mode="credit", credit_min=500, credit_max=600)
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
    bot, _ = _bot(tmp_path, monkeypatch, pair_mode="credit", credit_min=1, credit_max=300, sell_max_premium=0,
                  buy_min_premium=0, buy_max_premium=0)
    bot.handle_events([EV])
    combo = bot.open_combos()[0]
    bot._close(combo, "STRATEGY_EXIT")
    events = bot.storage.find("events")
    rows = signal_view.signal_rows(events, bot.storage.find("combos"), bot.storage.find("signal_actions"))
    assert len(rows) == 1
    r = rows[0]
    assert r["signal"] == "BUY" and r["action"] == "TRADED" and r["BUY leg"].endswith("CE") and r["SELL leg"].endswith("PE")
    assert r["net premium"].startswith("CREDIT") and r["exit reason"] == "STRATEGY_EXIT" and r["P&L ₹"] is not None
    legs = signal_view.leg_rows(bot.storage.find("combos")[0])
    assert {x["broker"] for x in legs} == {"paper-upstox", "paper-zerodha"}
    assert all(x["BUY entry"] and x["SELL entry"] and x["P&L ₹"] is not None for x in legs)


def test_signal_rows_work_for_old_data_without_decision_log():
    ev = {"kind": "ENTRY", "time": "2026-11-09T10:00", "direction": -1, "trigger": "CHoCH", "price": 23480.0,
          "sl": 23560.0, "target": 23320.0, "rr": 2.0, "mode": "paper"}
    combo = {"id": "x", "signal_time": "2026-11-09T10:00", "direction": -1, "trigger": "CHoCH", "status": "CLOSED",
             "expiry": "2026-11-10", "buy": {"strike": 23500.0, "type": "PE", "scan_ltp": 130.0},
             "sell": {"strike": 23350.0, "type": "CE", "scan_ltp": 100.0}, "scan_diff": 30.0,
             "legs": {"paper-upstox": {"buy": {"entry": 131.0, "qty": 75}, "sell": {"entry": 99.0, "qty": 75}, "pnl": 500.0}},
             "pnl": {"paper-upstox": 500.0}, "exit_reason": "TARGET"}
    r = signal_view.signal_rows([ev], [combo], [])[0]
    assert r["action"] == "TRADED" and r["net premium"] == "DEBIT 32.00" and r["P&L ₹"] == 500.0
    nothing = signal_view.signal_rows([ev], [], [])[0]
    assert nothing["action"].startswith("NOT TRADED")
