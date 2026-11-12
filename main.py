"""Nexus SMC Fractal Engine (Nifty) – options bot entry point.

  python main.py run                      # paper/live trading (mode from settings.yaml, or --mode)
  python main.py login upstox|zerodha     # daily broker login (saves today's token)
  python main.py backtest --csv file.csv  # backtest / TradingView parity check on CSV
  python main.py backtest --days 60       # backtest on broker history
  python main.py scan --side BUY          # dry-run: show which option pair would be traded right now
  python main.py status                   # open combos + today's trades
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse

from bot.config import ROOT, env, load_settings
from bot.services.notifier import Notifier
from bot.services.storage import make_storage


def setup_logging():
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-9s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = RotatingFileHandler(ROOT / "logs" / "bot.log", maxBytes=5_000_000, backupCount=10, encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers[:] = [sh, fh]
    for noisy in ("urllib3", "pymongo"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def storage_from(S):
    return make_storage(S.storage.get("backend", "mongo"), env("MONGO_URI"), env("MONGO_DB", "nexus_smc_bot"),
                        ROOT / "data_cache")


def cmd_run(S, args):
    from bot.live.runner import LiveBot
    mode = args.mode or S.bot.mode
    if mode not in ("paper", "live"):
        sys.exit("mode must be paper or live for `run`")
    if mode == "live":
        print("LIVE MODE: real orders will be placed on", S.brokers.enabled)
    st = storage_from(S)
    nt = Notifier(S.alerts.get("telegram_enabled", True), env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"))
    bot = LiveBot(S, st, nt, mode)
    import os
    import signal

    def _sig(*_):   # SIGTERM / SIGBREAK (Windows) -> graceful stop
        bot.running = False
        from bot.live.runner import STOP_FILE
        STOP_FILE.write_text("signal", encoding="utf-8")
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _sig)
            except Exception:  # noqa
                pass
    try:
        bot.run()
    finally:
        logging.shutdown()
        if os.environ.get("NEXUS_FROM_DASHBOARD"):
            os._exit(0)   # never linger on stray threads/connections - lets the dashboard start again


def cmd_login(S, args):
    from bot.services.tokens import save_token
    st = storage_from(S)
    if args.broker == "upstox":
        from bot.brokers.upstox import UpstoxBroker
        key, sec, red = env("UPSTOX_API_KEY"), env("UPSTOX_API_SECRET"), env("UPSTOX_REDIRECT_URI")
        if not (key and sec and red):
            sys.exit("Set UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI in .env")
        print("1) Open this URL and log in:\n", UpstoxBroker.login_url(key, red))
        raw = input("2) Paste the full redirected URL (or just the code): ").strip()
        code = parse_qs(urlparse(raw).query).get("code", [raw])[0]
        tok = UpstoxBroker.exchange_code(key, sec, red, code)
    else:
        from bot.brokers.zerodha import ZerodhaBroker
        key, sec = env("KITE_API_KEY"), env("KITE_API_SECRET")
        if not (key and sec):
            sys.exit("Set KITE_API_KEY, KITE_API_SECRET in .env")
        print("1) Open this URL and log in:\n", ZerodhaBroker.login_url(key))
        raw = input("2) Paste the full redirected URL (or just the request_token): ").strip()
        rt = parse_qs(urlparse(raw).query).get("request_token", [raw])[0]
        tok = ZerodhaBroker.exchange_request_token(key, sec, rt)
    save_token(st, args.broker, tok)
    print(f"{args.broker} token saved for today.")


def cmd_backtest(S, args):
    from backtest.runner import load_csv, run_backtest
    out = ROOT / "logs" / "backtest"
    if args.csv:
        candles, rows = load_csv(args.csv)
    else:
        from bot.data.candle import now_ist, session_open_dt
        from bot.live.runner import build_broker
        st = storage_from(S)
        b = build_broker(S.brokers.data_source, st)
        b.connect()
        b.load_instruments()
        inst = b.signal_instrument(S.bot.signal_symbol)
        n = now_ist()
        candles = b.candles_1m(inst, session_open_dt(n - timedelta(days=args.days)), n)
        rows = None
    res = run_backtest(candles, S.strategy, rows, out)
    print(f"\nBars processed: {res['bars']} (input TF {res['input_tf']}m -> chart TF {S.strategy.chart_timeframe}m)")
    print("Stats:", res["stats"])
    for t in res["trades"][-15:]:
        print(f"  {'BUY ' if t.direction == 1 else 'SELL'} {t.trigger:<9} {t.entry_time[:16]} @ {t.entry:.2f} "
              f"SL {t.sl:.2f} TGT {t.target:.2f} -> {t.reason:<6} {t.exit_time[:16] if t.exit_time else ''} "
              f"pts {t.pnl_points or 0:+.2f}  R {t.net_r or 0:+.2f}")
    if "parity" in res:
        print("\nPARITY vs TradingView:", {k: v for k, v in res["parity"].items() if k != "first_mismatches"})
        for m in res["parity"]["first_mismatches"]:
            print("  ", m)
    print(f"\nFiles: {out / 'backtest_trades.csv'} , {out / 'backtest_bars.csv'}")


def cmd_scan(S, args):
    from bot.data.candle import now_ist
    from bot.hedge.expiry import select_expiry
    from bot.hedge.scanner import find_pair, strike_window
    from bot.live.runner import build_broker
    st = storage_from(S)
    b = build_broker(S.brokers.data_source, st)
    b.connect()
    b.load_instruments()
    H = S.hedge
    exp = select_expiry(b.option_expiries(), H.expiry_mode, now_ist().date(), H.get("expiry_date", ""),
                        bool(H.get("use_next_expiry_on_expiry_day", True)))
    inst = b.signal_instrument(S.bot.signal_symbol)
    ref = b.ltp([inst]).get(inst.key)
    _, strikes = strike_window(ref, int(H.strike_step), int(H.strike_scan_width))
    chain = b.option_chain(exp, strikes)
    d = 1 if args.side.upper() == "BUY" else -1
    best, matches = find_pair(d, chain, ref, H)
    print(f"Reference {S.bot.signal_symbol}: {ref} | expiry {exp} | range {H.range_min}-{H.range_max} | quotes {len(chain)}")
    if not best:
        print("No pair in range right now.")
    for i, p in enumerate(matches[:10]):
        print(("=> " if i == 0 else "   ") + p.describe())


def cmd_status(S, args):
    st = storage_from(S)
    for c in st.find("combos"):
        if c.get("status") in ("OPEN", "CLOSE_FAILED") or args.all:
            print(c.get("id"), c.get("status"), c.get("side"), c.get("buy"), c.get("sell"), c.get("expiry"),
                  c.get("exit_reason", ""), c.get("pnl", ""))


def main():
    setup_logging()
    p = argparse.ArgumentParser(description="Nexus SMC Nifty options bot")
    p.add_argument("--settings", default=None, help="path to settings.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--mode", choices=["paper", "live"])
    lg = sub.add_parser("login"); lg.add_argument("broker", choices=["upstox", "zerodha"])
    bt = sub.add_parser("backtest"); bt.add_argument("--csv"); bt.add_argument("--days", type=int, default=60)
    sc = sub.add_parser("scan"); sc.add_argument("--side", default="BUY")
    ss = sub.add_parser("status"); ss.add_argument("--all", action="store_true")
    args = p.parse_args()
    S = load_settings(args.settings)
    {"run": cmd_run, "login": cmd_login, "backtest": cmd_backtest, "scan": cmd_scan, "status": cmd_status}[args.cmd](S, args)


if __name__ == "__main__":
    main()
