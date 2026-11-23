from bot.live.runner import combo_status_line, trade_id
from bot.strategy.engine import Event


def ev(d=1, trig="OB", t="2026-11-23T10:15:00+05:30"):
    return Event("ENTRY", 5, t, d, trig, 23500.0)


def test_trade_id_is_stable_and_readable():
    assert trade_id(ev()) == "B2311-1015-OB"
    assert trade_id(ev(-1, "CHoCH+IDM")) == "S2311-1015-CHOCH+IDM"
    assert trade_id(ev()) == trade_id(ev())
    assert trade_id(ev(t="2026-11-23 10:20:00")) == "B2311-1020-OB"


def test_same_bar_different_triggers_get_different_ids():
    assert trade_id(ev(trig="OB")) != trade_id(ev(trig="FVG"))


def test_status_line_shows_tp1_state():
    c = {"tid": "B2311-1015-OB", "side": "BUY", "status": "OPEN", "sl": 23430.0, "target": 23605.0, "expiry": "2026-11-26"}
    assert "TP1 pending" in combo_status_line(c)
    assert "TP1 hit" in combo_status_line({**c, "tp1_hit": True})
    done = combo_status_line({**c, "status": "CLOSED", "exit_reason": "TARGET", "pnl": {"upstox": 1500.0}})
    assert "CLOSED" in done and "TARGET" in done and "1,500" in done
