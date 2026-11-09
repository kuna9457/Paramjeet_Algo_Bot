from datetime import date

from bot.brokers.base import Instrument, OptionQuote
from bot.config import Section
from bot.hedge.executor import ComboExecutor
from bot.hedge.expiry import select_expiry
from bot.hedge.scanner import find_pair, strike_window
from bot.services.storage import LocalStorage
from tests.fakes import FakeBroker

CFG = Section({"range_min": 20, "range_max": 30, "strike_step": 50, "strike_scan_width": 10,
               "buy_strike_rule": "any", "sell_strike_rule": "ends_with_50", "min_leg_premium": 5})


def q(k, t, p):
    return OptionQuote(float(k), t, p, Instrument(f"{k}{t}", f"{k}{t}", None, float(k), t))


def test_client_example_buy_signal():
    chain = [q(23500, "CE", 130), q(23350, "PE", 100), q(23450, "PE", 128), q(23400, "CE", 175),
             q(23600, "CE", 85), q(23250, "PE", 62)]
    best, matches = find_pair(1, chain, 23480, CFG)
    assert {(p.buy.strike, p.sell.strike) for p in matches} == {(23500, 23350), (23600, 23250)}
    assert (best.buy.strike, best.buy.opt_type, best.sell.strike, best.sell.opt_type) == (23500, "CE", 23350, "PE")
    assert best.diff == 30


def test_sell_leg_never_on_00_strike_and_diff_positive():
    chain = [q(23500, "CE", 130), q(23400, "PE", 105), q(23300, "PE", 70)]   # only 00 puts
    best, _ = find_pair(1, chain, 23480, CFG)
    assert best is None
    chain = [q(23500, "CE", 100), q(23550, "PE", 125)]                      # negative diff (credit)
    assert find_pair(1, chain, 23480, CFG)[0] is None


def test_sell_signal_mapping():
    chain = [q(23450, "PE", 110), q(23650, "CE", 85), q(23550, "CE", 140)]
    best, _ = find_pair(-1, chain, 23480, CFG)
    assert best.buy.opt_type == "PE" and best.sell.opt_type == "CE" and best.sell.strike == 23650


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
    pair, _ = find_pair(1, chain, up.spot, Section({**CFG.__dict__, "range_min": 0.5, "range_max": 500}))
    assert pair and int(pair.sell.strike) % 100 == 50
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
