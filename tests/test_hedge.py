from datetime import date

from bot.brokers.base import Instrument, OptionQuote
from bot.config import Section
from bot.hedge.executor import ComboExecutor
from bot.hedge.expiry import select_expiry
from bot.hedge.scanner import find_pair, strike_window
from bot.services.storage import LocalStorage
from tests.fakes import FakeBroker

CFG = Section({"sell_min_premium": 0, "sell_max_premium": 55, "hedge_diff_min": 10, "hedge_diff_max": 15,
               "strike_step": 50, "strike_scan_width": 10, "buy_strike_rule": "ends_with_50",
               "sell_strike_rule": "ends_with_50", "min_leg_premium": 5})


def q(k, t, p):
    return OptionQuote(float(k), t, p, Instrument(f"{k}{t}", f"{k}{t}", None, float(k), t))


def test_client_example_buy_signal_sells_put_le_55_and_hedge_gap_in_range():
    chain = [q(23450, "PE", 98), q(23350, "PE", 70), q(23250, "PE", 54), q(23200, "PE", 46), q(23150, "PE", 39),
             q(23050, "PE", 28), q(23600, "CE", 40)]
    best, matches = find_pair(1, chain, 23480, CFG)
    assert (best.sell.strike, best.sell.opt_type, best.buy.strike, best.buy.opt_type) == (23250, "PE", 23150, "PE")
    assert best.net == 15                                   # 54 - 39 = 15, inside 10-15
    assert {(m.sell.strike, m.buy.strike) for m in matches} == {(23250, 23150), (23150, 23050)}   # 39 - 28 = 11


def test_sell_leg_premium_cap_and_strike_rule():
    hedge = q(23050, "PE", 42)
    assert find_pair(1, [q(23250, "PE", 55), hedge], 23480, CFG)[0] is not None      # 55 allowed, gap 13
    assert find_pair(1, [q(23250, "PE", 56), hedge], 23480, CFG)[0] is None          # above the 55 cap
    assert find_pair(1, [q(23300, "PE", 52), hedge], 23480, CFG)[0] is None          # 00 strike never sold
    assert find_pair(1, [q(23250, "PE", 52), q(23050, "CE", 40)], 23480, CFG)[0] is None   # hedge must be a PUT


def test_sell_picks_richest_premium_then_hedge_by_gap():
    chain = [q(23350, "PE", 54), q(23250, "PE", 45), q(23150, "PE", 41), q(23050, "PE", 36)]
    best, matches = find_pair(1, chain, 23480, CFG)
    assert (best.sell.strike, best.buy.strike) == (23350, 23150)      # 23250 gap 9, 23150 gap 13, 23050 gap 18
    assert len(matches) == 1


def test_hedge_must_be_beyond_the_sold_strike_and_gap_inside_range():
    sell = q(23250, "PE", 50)
    assert find_pair(1, [sell, q(23350, "PE", 38)], 23480, CFG)[0] is None     # hedge above the sold put
    assert find_pair(1, [sell, q(23150, "PE", 41)], 23480, CFG)[0] is None     # gap 9 < 10
    assert find_pair(1, [sell, q(23150, "PE", 34)], 23480, CFG)[0] is None     # gap 16 > 15
    assert find_pair(1, [sell, q(23150, "PE", 40)], 23480, CFG)[0] is not None  # gap 10 (edge included)
    assert find_pair(1, [sell, q(23150, "PE", 35)], 23480, CFG)[0] is not None  # gap 15 (edge included)


def test_sell_signal_mapping():
    chain = [q(23450, "PE", 110), q(23650, "CE", 85), q(23750, "CE", 53), q(23850, "CE", 40), q(23550, "CE", 140)]
    best, _ = find_pair(-1, chain, 23480, CFG)
    assert (best.sell.strike, best.sell.opt_type, best.buy.strike, best.buy.opt_type) == (23750, "CE", 23850, "CE")


def test_sell_min_premium_and_zero_means_no_limit():
    chain = [q(23250, "PE", 30), q(23150, "PE", 20)]
    open_cfg = Section({**CFG.__dict__, "sell_max_premium": 0, "hedge_diff_min": 0, "hedge_diff_max": 0})
    assert find_pair(1, chain, 23480, open_cfg)[0] is not None
    assert find_pair(1, chain, 23480, Section({**open_cfg.__dict__, "sell_min_premium": 31}))[0] is None


def test_strike_window():
    atm, w = strike_window(23480, 50, 2)
    assert atm == 23500 and w == [23400, 23450, 23500, 23550, 23600]


def test_expiry_selection():
    ex = [date(2026, 11, 10), date(2026, 11, 17), date(2026, 11, 24), date(2026, 12, 29), date(2027, 1, 26)]
    assert select_expiry(ex, "nearest_weekly", date(2026, 11, 9)) == date(2026, 11, 10)
    assert select_expiry(ex, "nearest_weekly", date(2026, 11, 10)) == date(2026, 11, 17)   # expiry day -> next
    assert select_expiry(ex, "nearest_weekly", date(2026, 11, 10), use_next_on_expiry_day=False) == date(2026, 11, 10)
    assert select_expiry(ex, "next_weekly", date(2026, 11, 9)) == date(2026, 11, 17)
    assert select_expiry(ex, "current_monthly", date(2026, 11, 9)) == date(2026, 11, 24)
    assert select_expiry(ex, "next_monthly", date(2026, 11, 9)) == date(2026, 12, 29)
    assert select_expiry(ex, "fixed_date", date(2026, 11, 9), "2026-12-29") == date(2026, 12, 29)


def test_executor_open_and_close_on_two_brokers(tmp_path):
    up, ze = FakeBroker(), FakeBroker()
    bcfg = Section({"lots": {"upstox": 2, "zerodha": 1}, "order_type": "LIMIT", "limit_buffer_pts": 2,
                    "leg_order": "buy_first", "leg_fill_timeout_sec": 1, "leg_retry_count": 1, "freeze_qty": 1800,
                    "product": "NRML"})
    ex = ComboExecutor({"upstox": up, "zerodha": ze}, bcfg, LocalStorage(tmp_path), None)
    _, strikes = strike_window(up.spot, 50, 10)
    chain = up.option_chain(date(2026, 11, 17), strikes)
    pair, _ = find_pair(1, chain, up.spot, Section({**CFG.__dict__, "hedge_diff_min": 0, "hedge_diff_max": 0}))
    assert pair and int(pair.sell.strike) % 100 == 50 and pair.buy.strike < pair.sell.strike
    combo = ex.open_combo(pair, date(2026, 11, 17), {"trigger": "IDM", "price": 23480, "sl": 23400, "target": 23640})
    assert combo["status"] == "OPEN"
    assert combo["legs"]["upstox"]["buy"]["qty"] == 150 and combo["legs"]["zerodha"]["sell"]["qty"] == 75
    assert up.placed[0][0] == "BUY" and up.placed[1][0] == "SELL"          # buy leg first
    up.spot = ze.spot = 23560                                               # market moves up
    closed = ex.close_combo(combo, "TARGET")
    assert closed["status"] == "CLOSED"
    assert up.placed[2][0] == "BUY" and up.placed[2][1] == combo["legs"]["upstox"]["sell"]["symbol"]  # short covered first
    assert closed["pnl"]["upstox"] > 0 and closed["pnl"]["zerodha"] > 0
    assert LocalStorage(tmp_path).find_one("combos", {"id": combo["id"]})["status"] == "CLOSED"
