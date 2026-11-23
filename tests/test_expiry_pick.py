from datetime import date

from bot.hedge.expiry import choose_expiry, expiry_options, load_expiry_list, save_expiry_list
from bot.services.storage import LocalStorage

EXP = [date(2026, 11, 26), date(2026, 12, 3), date(2026, 12, 10), date(2026, 12, 29)]
TODAY = date(2026, 11, 23)


def test_default_is_nearest_upcoming():
    assert choose_expiry(EXP, "nearest_weekly", TODAY) == (date(2026, 11, 26), "")


def test_on_expiry_day_nearest_moves_to_the_next_one():
    assert choose_expiry(EXP, "nearest_weekly", date(2026, 11, 26))[0] == date(2026, 12, 3)


def test_exact_date_is_used_when_listed():
    assert choose_expiry(EXP, "fixed_date", TODAY, "2026-12-10") == (date(2026, 12, 10), "")


def test_exact_date_not_listed_falls_back_to_nearest_with_note():
    exp, note = choose_expiry(EXP, "fixed_date", TODAY, "2026-11-12")
    assert exp == date(2026, 11, 26) and "2026-11-12" in note


def test_options_label_weekly_and_monthly():
    o = expiry_options(EXP, TODAY)
    assert [x["date"] for x in o][0] == "2026-11-26"
    assert "monthly" in o[-1]["label"] and "weekly" in o[1]["label"] and "monthly" in o[0]["label"]
    assert expiry_options(EXP, date(2026, 12, 4))[0]["date"] == "2026-12-10"


def test_expiry_list_roundtrip(tmp_path):
    st = LocalStorage(tmp_path)
    save_expiry_list(st, "NIFTY_SPOT", EXP)
    assert load_expiry_list(st) == EXP
