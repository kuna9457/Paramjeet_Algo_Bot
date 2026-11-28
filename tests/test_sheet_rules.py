"""Client sheet rules for the option legs: expiry with >= 7 days left, sell ATM / 50 OTM + hedge 200 OTM,
bid-ask <= Rs 0.30, sell-leg premium min / max, 2 lots."""
from datetime import date, datetime
from pathlib import Path

import pytest

import bot.live.runner as runner_mod
from bot.brokers.base import Instrument, OptionQuote
from bot.config import Section, load_settings
from bot.data.candle import IST
from bot.hedge.expiry import expiry_for, select_expiry
from bot.hedge.scanner import find_pair, range_text
from bot.strategy.engine import Event
from bot.ui import settings_io
from tests.test_runner import make_bot

ATM = Section({"strike_mode": "atm_offset", "sell_otm_points": 50, "hedge_otm_points": 200,
               "atm_sell_min_premium": 0, "atm_sell_max_premium": 0, "max_bid_ask_spread": 0.30,
               "strike_step": 50, "strike_scan_width": 20, "min_leg_premium": 5})


def q(k, t, p, bid=0.0, ask=0.0):
    return OptionQuote(float(k), t, p, Instrument(f"{k}{t}", f"{k}{t}", None, float(k), t), bid, ask)


def cfg(**kw):
    return Section({**ATM.__dict__, **kw})


# ------------------------------------------------------------------ expiry
EX = [date(2026, 11, 10), date(2026, 11, 17), date(2026, 11, 24), date(2026, 12, 1), date(2026, 12, 29)]


@pytest.mark.parametrize("today, want", [(date(2026, 11, 9), date(2026, 11, 17)),     # 1 DTE skipped, 8 DTE taken
                                         (date(2026, 11, 10), date(2026, 11, 17)),    # 0 DTE never, 7 DTE ok
                                         (date(2026, 11, 11), date(2026, 11, 24)),    # 6 DTE too close
                                         (date(2026, 11, 28), date(2026, 12, 29))])   # 3 DTE -> monthly 31 DTE
def test_first_expiry_with_at_least_7_days(today, want):
    assert select_expiry(EX, "min_days", today, min_days=7) == want


def test_max_days_and_no_expiry_note():
    assert select_expiry(EX, "min_days", date(2026, 11, 28), min_days=7, max_days=15) is None
    exp, note = expiry_for(EX, Section({"expiry_mode": "min_days", "min_days_to_expiry": 7, "max_days_to_expiry": 15}),
                           date(2026, 11, 28))
    assert exp is None and "7-15 days" in note
    exp, _ = expiry_for(EX, Section({"expiry_mode": "min_days", "min_days_to_expiry": 7}), date(2026, 11, 9))
    assert exp == date(2026, 11, 17)


# ------------------------------------------------------------------ strikes (sheet example A / B)
def chain_example():
    pe = {25950: 160, 25900: 130, 25850: 105, 25800: 84, 25750: 66, 25700: 52}
    ce = {25850: 162, 25900: 131, 25950: 104, 26000: 83, 26050: 65, 26100: 51}
    return [q(k, "PE", p, p - 0.1, p + 0.1) for k, p in pe.items()] + [q(k, "CE", p, p - 0.1, p + 0.1) for k, p in ce.items()]


def test_buy_signal_sells_atm_or_50_otm_put_and_hedges_200_below_atm():
    best, matches = find_pair(1, chain_example(), 25900.0, ATM)
    assert {(m.sell.strike, m.buy.strike) for m in matches} == {(25900, 25700), (25850, 25700)}
    assert (best.sell.strike, best.buy.strike, best.sell.opt_type) == (25900, 25700, "PE")    # richest = ATM
    assert best.net == 78                                                                   # 130 - 52


def test_sell_signal_mirror_with_calls():
    best, matches = find_pair(-1, chain_example(), 25900.0, ATM)
    assert {(m.sell.strike, m.buy.strike) for m in matches} == {(25900, 26100), (25950, 26100)}
    assert best.buy.opt_type == best.sell.opt_type == "CE"


def test_sell_leg_premium_min_max_picks_the_strike():
    best, _ = find_pair(1, chain_example(), 25900.0, cfg(atm_sell_max_premium=120))
    assert best.sell.strike == 25850                      # ATM 130 is above the max -> 50 OTM (105)
    assert find_pair(1, chain_example(), 25900.0, cfg(atm_sell_min_premium=140))[0] is None
    assert find_pair(1, chain_example(), 25900.0, cfg(sell_otm_points=0))[1][0].sell.strike == 25900


def test_bid_ask_spread_filter():
    ch = chain_example()
    ch = [q(25900, "PE", 130, 129.5, 130.5) if (x.strike, x.opt_type) == (25900, "PE") else x for x in ch]
    best, matches = find_pair(1, ch, 25900.0, ATM)
    assert [m.sell.strike for m in matches] == [25850]   # ATM spread 1.00 > 0.30 -> skipped
    wide_hedge = [q(25700, "PE", 52, 51.0, 52.5) if (x.strike, x.opt_type) == (25700, "PE") else x for x in ch]
    assert find_pair(1, wide_hedge, 25900.0, ATM)[0] is None
    assert find_pair(1, wide_hedge, 25900.0, cfg(max_bid_ask_spread=0))[0] is not None    # 0 = off
    no_depth = [q(x.strike, x.opt_type, x.ltp) for x in ch]
    assert find_pair(1, no_depth, 25900.0, ATM)[0] is not None                             # no depth data -> allowed
    assert "bid-ask 0.20" in find_pair(1, chain_example(), 25900.0, ATM)[0].describe()


# ------------------------------------------------------------------ strike endings: sold 00, hedge 50
ENDINGS = {"atm_sell_strike_rule": "ends_with_00", "atm_buy_strike_rule": "ends_with_50"}


def full_chain():
    out = []
    for k in range(25500, 26350, 50):
        for t in ("PE", "CE"):
            p = max(5.0, 130 - abs(k - 25900) * 0.4)
            out.append(q(k, t, p, p - 0.1, p + 0.1))
    return out


@pytest.mark.parametrize("spot, d, sell, hedge", [
    (25910.0, 1, 25900, 25750),     # ATM 25900: sell 25900 PE, hedge 25700 ends 00 -> 25750 (nearer ATM)
    (25940.0, 1, 25900, 25750),     # ATM 25950: sell 25900 PE (50 OTM, ends 00), hedge 25750 exactly 200 OTM
    (25910.0, -1, 25900, 26050),    # calls mirror: hedge 26100 ends 00 -> 26050
    (25940.0, -1, 26000, 26150),    # ATM 25950: sell 26000 CE, hedge 26150 exactly 200 OTM
])
def test_sold_strike_ends_00_and_hedge_strike_ends_50(spot, d, sell, hedge):
    best, matches = find_pair(d, full_chain(), spot, cfg(**ENDINGS))
    assert (best.sell.strike, best.buy.strike) == (sell, hedge)
    assert all(int(m.sell.strike) % 100 == 0 and int(m.buy.strike) % 100 == 50 for m in matches)
    assert 150 <= abs(best.sell.strike - best.buy.strike) <= 250


def test_sell_otm_0_with_an_ending_rule_is_rejected(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text(Path(settings_io.DEFAULT_SETTINGS).read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError):
        settings_io.save({("hedge", "sell_otm_points"): 0}, p)
    assert load_settings(p).hedge.atm_sell_strike_rule == "ends_with_00"
    assert load_settings(p).hedge.atm_buy_strike_rule == "ends_with_50"


def test_range_text():
    assert range_text(ATM) == "sell ATM to 50 OTM, hedge ~200 OTM, bid-ask <= 0.3"
    assert range_text(cfg(atm_sell_max_premium=120)) == "sell ATM to 50 OTM (premium <= 120), hedge ~200 OTM, bid-ask <= 0.3"
    assert range_text(cfg(**ENDINGS)) == "sell ATM to 50 OTM ends_with_00, hedge ~200 OTM ends_with_50, bid-ask <= 0.3"


# ------------------------------------------------------------------ settings
def test_shipped_settings_follow_the_sheet():
    S = load_settings()
    h = S.hedge
    assert (h.expiry_mode, h.min_days_to_expiry, h.strike_mode, h.sell_otm_points, h.hedge_otm_points,
            h.max_bid_ask_spread) == ("min_days", 7, "atm_offset", 50, 200, 0.30)
    assert S.brokers.lots == {"upstox": 2, "zerodha": 2}


def test_validation(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text(Path(settings_io.DEFAULT_SETTINGS).read_text(encoding="utf-8"), encoding="utf-8")
    for bad in ({("hedge", "min_days_to_expiry"): 1}, {("hedge", "hedge_otm_points"): 225},
                {("hedge", "hedge_otm_points"): 50}, {("hedge", "atm_sell_min_premium"): 200, ("hedge", "atm_sell_max_premium"): 100},
                {("hedge", "max_bid_ask_spread"): -1}, {("hedge", "strike_mode"): "delta"}):
        with pytest.raises(ValueError):
            settings_io.save(bad, p)
    settings_io.save({("hedge", "atm_sell_max_premium"): 150}, p)
    assert load_settings(p).hedge.atm_sell_max_premium == 150


# ------------------------------------------------------------------ end to end with the fake broker
def test_trade_end_to_end_with_sheet_rules(tmp_path, monkeypatch):
    bot, fake = make_bot(tmp_path)
    for k, v in {**ATM.__dict__, "expiry_mode": "min_days", "min_days_to_expiry": 7}.items():
        setattr(bot.S.hedge, k, v)
    monkeypatch.setattr(runner_mod, "now_ist", lambda: datetime(2026, 11, 9, 10, 0, tzinfo=IST))
    from bot.strategy.engine import NexusEngine
    bot.engine = NexusEngine(bot.S.strategy)
    fake.spot = 23480.0
    bot.handle_events([Event("ENTRY", 100, "2026-11-09T10:00", 1, "OB", 23480.0, 23390.0, 23592.5, 23529.5, 1.25)])
    c = bot.open_combos()[0]
    assert c["expiry"] == "2026-11-17"                                # 8 days left, not the 1-day expiry
    assert (c["sell"]["strike"], c["buy"]["strike"], c["sell"]["type"]) == (23500, 23350, "PE")   # sold 00, hedge 50
