from pathlib import Path

from bot.config import load_settings

BASELINE = Path(__file__).parent / "data" / "settings_baseline.yaml"
from bot.services import messages as M
from bot.services.preview import simulate
from bot.strategy.engine import Event


def test_signal_message_has_everything():
    ev = Event("ENTRY", 1, "2026-11-23T10:15:00+05:30", 1, "OB", 23500.0, 23420.0, 23620.0, 23580.0, 1.5)
    t = M.signal_msg("B2311-1015-OB", ev, "v8.7")
    for part in ("BUY SIGNAL", "B2311-1015-OB", "Order Block", "23,500.00", "23,420.00", "TP1", "23,620.00", "v8.7"):
        assert part in t


def test_sl_trail_shows_old_and_new():
    t = M.sl_move_msg("X", 23420.0, 23500.0, 23500.0)
    assert "23,420.00" in t and "23,500.00" in t and "break-even" in t


def test_sample_buy_and_sell_flows_show_legs_qty_and_ids():
    S = load_settings(BASELINE)
    for side, buy_t, sell_t in (("BUY", "CE", "PE"), ("SELL", "PE", "CE")):
        msgs = simulate(side, S)
        text = "\n".join(msgs)
        assert all(m.startswith("🧪 TEST") for m in msgs)
        assert f"{side} SIGNAL" in text and "TP1 REACHED" in text and "STOP-LOSS TRAILED" in text
        assert "POSITION CLOSED" in text and "qty" in text and "[" in text
        if "No option pair" not in text:
            assert f"BUY  NIFTY" in text and buy_t in text and sell_t in text


def test_one_message_per_side_is_telegram_sized():
    from bot.services.preview import simulate_text
    S = load_settings(BASELINE)
    for side in ("BUY", "SELL"):
        t = simulate_text(side, S)
        assert t.startswith("🧪 TEST") and len(t) < 4000 and t.count("🧪") == 1
