"""End-to-end paper flow with a fake broker (no network)."""
from datetime import datetime, timedelta

import bot.live.runner as runner_mod
from bot.brokers.paper import PaperBroker
from bot.config import load_settings
from bot.data.candle import IST, resample
from bot.hedge.executor import ComboExecutor
from bot.live.runner import LiveBot
from bot.services.notifier import Notifier
from bot.services.storage import LocalStorage
from bot.strategy.engine import Event
from tests.fakes import FakeBroker, synthetic_1m


def make_bot(tmp_path):
    S = load_settings()
    S.hedge.range_min, S.hedge.range_max = 1, 500
    S.hedge.max_rescan_sec = 0
    S.brokers.leg_fill_timeout_sec = 1
    fake = FakeBroker()
    bot = LiveBot(S, LocalStorage(tmp_path), Notifier(False, "", ""), "paper")
    bot.data = fake
    bot.exec_brokers = {"paper-upstox": PaperBroker(fake, "upstox"), "paper-zerodha": PaperBroker(fake, "zerodha")}
    bot.executor = ComboExecutor(bot.exec_brokers, S.brokers, bot.storage, bot.notify)
    bot.signal_inst = fake.signal_instrument("NIFTY_SPOT")
    return bot, fake


def test_signal_to_combo_to_tick_sl_exit(tmp_path, monkeypatch):
    bot, fake = make_bot(tmp_path)
    monkeypatch.setattr(runner_mod, "now_ist", lambda: datetime(2026, 11, 9, 10, 0, tzinfo=IST))
    from bot.strategy.engine import NexusEngine
    bot.engine = NexusEngine(bot.S.strategy)

    # strategy says BUY at 23480, SL 23400, target 23640
    bot.engine.activeTradeDir, bot.engine.slPrice, bot.engine.targetPrice = 1, 23400.0, 23640.0
    ev = Event("ENTRY", 100, "2026-11-09T10:00", 1, "IDM", 23480.0, 23400.0, 23640.0, 23560.0, 2.0)
    bot.handle_events([ev])
    combos = bot.open_combos()
    assert len(combos) == 1
    c = combos[0]
    assert c["side"] == "BUY" and c["buy"]["type"] == "CE" and c["sell"]["type"] == "PE"
    assert int(c["sell"]["strike"]) % 100 == 50
    assert c["expiry"] == "2026-11-10"
    assert set(c["legs"]) == {"paper-upstox", "paper-zerodha"}

    fake.spot = 23390.0          # Nifty breaks SL intrabar
    bot.monitor()
    assert not bot.open_combos()
    closed = bot.storage.find("combos")[0]
    assert closed["exit_reason"] == "SL_TICK" and closed["status"] == "CLOSED"
    assert all(v < 0 for v in closed["pnl"].values())


def test_expiry_day_eod_exit(tmp_path, monkeypatch):
    bot, fake = make_bot(tmp_path)
    from bot.strategy.engine import NexusEngine
    bot.engine = NexusEngine(bot.S.strategy)
    monkeypatch.setattr(runner_mod, "now_ist", lambda: datetime(2026, 11, 9, 10, 0, tzinfo=IST))
    bot.S.hedge.expiry_mode = "fixed_date"
    bot.S.hedge.expiry_date = "2026-11-10"
    bot.S.hedge.use_next_expiry_on_expiry_day = False
    bot.handle_events([Event("ENTRY", 1, "t", -1, "CHoCH", 23480.0, 23560.0, 23320.0, 23400.0, 2.0)])
    assert bot.open_combos()[0]["side"] == "SELL"
    monkeypatch.setattr(runner_mod, "now_ist", lambda: datetime(2026, 11, 10, 15, 16, tzinfo=IST))
    bot.engine.activeTradeDir = 0
    bot.monitor()
    assert bot.storage.find("combos")[0]["exit_reason"] == "EXPIRY_EOD"


def test_process_new_bars_and_strategy_exit_closes_combo(tmp_path, monkeypatch):
    bot, fake = make_bot(tmp_path)
    from bot.strategy.engine import NexusEngine
    hist = synthetic_1m(days=40)
    days = sorted({c.time.date() for c in hist})
    warm = [c for c in hist if c.time.date() < days[-1]]
    today = [c for c in hist if c.time.date() == days[-1]]
    bot.engine = NexusEngine(bot.S.strategy)
    for b in resample(warm, 5):
        bot.engine.on_bar(b)
    bot.last_bar_time = resample(warm, 5)[-1].time
    fake.candles_1m_today = lambda inst: today
    end = today[-1].time + timedelta(minutes=2)
    monkeypatch.setattr(runner_mod, "now_ist", lambda: end)
    seen = []
    bot.handle_events = lambda evs: seen.extend(evs)
    bot.process_new_bars()
    assert bot.engine.bar_index == len(resample(hist, 5)) - 1     # all 75 bars of the day processed
    assert bot.last_bar_time.strftime("%H:%M") == "15:25"
