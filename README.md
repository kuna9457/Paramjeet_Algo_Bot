# Nexus SMC Fractal Engine (Nifty) – Python Options Bot

Python port of the TradingView indicator `Algo_SMC_Suite_Nifty_v7.4`. It trades Nifty options combos on **Upstox** and **Zerodha**.

| Strategy signal (5m Nifty candle close) | Combo placed |
|---|---|
| **BUY**  | BUY CALL (any strike) + SELL PUT (strike ending in **50**) |
| **SELL** | BUY PUT (any strike) + SELL CALL (strike ending in **50**) |

The pair is chosen so that **Buy-leg LTP − Sell-leg LTP** is positive and inside your range (default **20–30**). If several pairs qualify, the one whose sell strike is **closest** to Nifty is used. Exits follow the strategy's SL / TP1 break-even / target / opposite CHoCH / max-bars rules on the Nifty chart, plus an EOD exit on the contract's expiry day. Both legs are always closed together, with the short leg bought back first.

---

## 1. Install (one time)
```bash
cd nexus_smc_bot
pip install -r requirements.txt
copy .env.example .env        # then fill in the values
```
`.env` needs:
- `MONGO_URI` (MongoDB Atlas or local). If it's missing, the bot stores data as JSON in `data_cache/`.
- Upstox: `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_REDIRECT_URI` (the same URI registered in the Upstox developer app)
- Zerodha: `KITE_API_KEY`, `KITE_API_SECRET` (a paid Kite Connect app; set its redirect URL in the Kite developer console)
- Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

> SEBI's algo rules: brokers only accept API orders from a **static IP registered with them**. Run the bot on a VPS with a static IP and register that IP in both the Upstox and Kite developer apps.

## 2. Every morning (tokens expire daily)
```bash
python main.py login upstox     # opens the URL, paste the redirected URL back
python main.py login zerodha
```
You can also paste tokens directly into `.env` (`UPSTOX_ACCESS_TOKEN`, `KITE_ACCESS_TOKEN`).

## 2b. Web dashboard (recommended)
```bash
streamlit run dashboard.py          # or double-click run_dashboard.bat
```
It opens at http://localhost:8501.

| Page | What you can do |
|---|---|
| 🤖 Bot control | Live status (running/stopped, mode, Nifty LTP, trend, strategy position with entry/SL/TP1/target), open combos with live MTM, today's P&L, live strategy chart. Buttons: **Start** (paper/live), **Stop** (graceful, positions stay open), **Force stop**, **Kill switch** (squares off everything), **Scan option chain** |
| ⚙️ Settings | Every setting from `settings.yaml` (strategy inputs use the TradingView names). Saving checks the settings, keeps the comments in the file and makes a backup (undo button). Restart the bot to apply |
| 🔑 Login & connections | Daily Upstox / Zerodha login (open the login page, paste the redirected URL), token status, test connection, MongoDB / Telegram status, test Telegram message, edit API keys in `.env` |
| 📜 History | All option combos (paper/live) with filters, P&L per broker, cumulative and daily P&L charts, strategy signals/events, CSV download |
| 📈 Backtest | Runs the strategy on broker history, a TradingView CSV or demo data. Shows the candle chart with every BUY/SELL entry, exit, SL and target, the protected strong high/low, a "one trade" zoom, equity curves (R and approximate ₹), tables by trigger/exit reason/month, and the TradingView parity result. Quick overrides let you test settings without saving them |
| 📄 Logs | Tail and filter `logs/bot.log` |

The bot started from the dashboard runs as a separate background process, so closing the browser does not stop it.
On a VPS, set `DASHBOARD_PASSWORD` in `.env` or open the dashboard only through an SSH tunnel.

Deploying the dashboard + paper bot on Render (free plan): see `DEPLOY_RENDER.md`.

## 3. Commands
| Command | What it does |
|---|---|
| `python main.py backtest --csv nifty_5m.csv` | Backtest on a TradingView CSV export. If the CSV contains the indicator's BUY/SELL columns, it also prints a **bar-by-bar parity report** |
| `python main.py backtest --days 60` | Backtest on broker history (1m candles resampled to your timeframe) |
| `python main.py scan --side BUY` | Dry run: shows which option pairs are in range right now. No orders |
| `python main.py run --mode paper` | Paper trading: live signals and real option prices, simulated fills |
| `python main.py run --mode live` | **Real orders** on all brokers in `brokers.enabled` |
| `python main.py status [--all]` | Open (or all) combos |
| `streamlit run dashboard.py` | Web dashboard (see 2b) |
| create a file named `KILL` in this folder | Kill switch: squares off everything and stops |
| create a file named `STOP` in this folder | Graceful stop (positions stay open) |

Output: `logs/bot.log`, `logs/backtest/backtest_trades.csv`, `logs/backtest/backtest_bars.csv` (every internal value per bar, for comparison with TradingView).

## 4. Settings – `config/settings.yaml`
Strategy keys use the same names as the Pine inputs. Restart the bot after changing settings.

| Area | Main keys |
|---|---|
| Chart | `bot.chart_timeframe` (default 5), `bot.signal_symbol` (NIFTY_SPOT / NIFTY_FUT), `bot.warmup_bars` |
| Brokers | `brokers.enabled`, `brokers.data_source`, `brokers.lots.upstox / zerodha`, `product` (NRML), `order_type`, `limit_buffer_pts`, `leg_order`, retries |
| Hedge | `hedge.expiry_mode` (nearest_weekly / next_weekly / current_monthly / next_monthly / fixed_date), `expiry_date`, `range_min`, `range_max`, `strike_scan_width`, `min_leg_premium`, `expiry_day_exit` + `expiry_exit_time`, `combo_sl_rs` / `combo_target_rs` (0 = off) |
| Safety | `entry_window_start/end`, `max_trades_per_day`, `max_daily_loss_rs`, `kill_switch` (all off by default) |
| Strategy | all Pine inputs: triggers (IDM / CHoCH / OB / FVG), filters, SL mode and points, min/max R:R, TP1/break-even, max hold bars, … |

## 5. How "exact" is checked
1. On TradingView, open the indicator on NSE:NIFTY 5m and choose **Export chart data**.
2. Run `python main.py backtest --csv export.csv` with the same settings.
3. The parity report lists every bar where TradingView and Python disagree on BUY/SELL. The target is 0.
4. `logs/backtest/backtest_bars.csv` has the trend, strong high/low, equilibrium, each trigger, SL and target for every bar, so any difference can be traced.

## 6. Layout
```
bot/pine/        Pine built-ins (RMA/EMA/ATR/DMI/pivots) + request.security emulation
bot/strategy/    engine.py – line-by-line port of the indicator
bot/hedge/       expiry selection, strike/premium scanner, 2-leg order executor
bot/brokers/     upstox.py, zerodha.py, paper.py
bot/live/        runner.py – warm-up, candle loop, tick SL/target, expiry exit, alerts
bot/services/    MongoDB/local storage, Telegram, daily tokens
bot/ui/          dashboard helpers (settings editor, process control, charts)
dashboard.py     Streamlit web dashboard
backtest/        CSV / broker backtest + TradingView parity
tests/           offline tests (python -m pytest)
```

## 7. Notes
- Pine quirks are reproduced on purpose so results match TradingView. Example: "OB Mitigation Style" has no effect in the Pine code. `strategy.fix_ob_mitigation: true` turns on the corrected behaviour.
- Upstox does not offer a sandbox and Kite has no paper mode. Run `--mode paper` for at least 1–2 weeks and compare each signal with TradingView alerts before going live with 1 lot.
- Buy CE + Sell PE behaves like a long future (and Buy PE + Sell CE like a short future). Keep enough NRML margin for the sold leg in both accounts.
