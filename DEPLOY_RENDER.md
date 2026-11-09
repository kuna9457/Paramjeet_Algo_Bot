# Deploy on Render (free plan, paper-trading demo)

The dashboard and the paper bot run together in one Render web service. **Paper mode only**: Render has no fixed
outgoing IP, so Upstox/Zerodha would reject real orders.

## 1. Before you start
- **MongoDB Atlas** (free M0 cluster). Create a database user, and under *Network Access* allow `0.0.0.0/0`
  (Render's outgoing IPs are not fixed). Copy the connection string (`mongodb+srv://...`).
- A **GitHub account** (a private repo is fine).
- Upstox and/or Kite developer apps. You will need the Render URL for their redirect settings (step 4).

## 2. Put the code on GitHub
Push the contents of the `nexus_smc_bot` folder so that `dashboard.py` and `render.yaml` are in the repo root.
`.env` is in `.gitignore`, so your keys are not uploaded.

## 3. Create the service
Render dashboard -> **New -> Blueprint** -> pick the repo. It reads `render.yaml` (plan: free, region: Singapore).
Fill in the environment variables it asks for:

| Variable | Value |
|---|---|
| `MONGO_URI` | Atlas connection string |
| `UPSTOX_API_KEY`, `UPSTOX_API_SECRET` | from the Upstox developer app |
| `UPSTOX_REDIRECT_URI` | your Render URL, e.g. `https://nexus-smc-bot.onrender.com` |
| `KITE_API_KEY`, `KITE_API_SECRET` | from the Kite developer app (optional) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional alerts |

`AUTO_START_BOT=paper` (already set) starts the paper bot every time the service boots.
Change it to `off` if you prefer to press Start in the dashboard yourself.

## 4. Redirect URLs
After the first deploy you get a URL like `https://nexus-smc-bot.onrender.com`.
- Upstox developer app -> set the redirect URL to exactly that URL, and use the same value in `UPSTOX_REDIRECT_URI`.
- Kite developer console -> set the redirect URL to the same URL.

## 5. Keep it awake (important on the free plan)
Free services **go to sleep after 15 minutes without a visit**, and the bot stops with them.
Create a free monitor on [UptimeRobot](https://uptimerobot.com) (or cron-job.org):
- Type: HTTP(s), URL: `https://<your-app>.onrender.com/_stcore/health`, interval: **5 minutes**.

A free service has about 750 hours per month, enough for one service running all month.
If you need a guarantee, switch the plan to Starter (about $7/month) in the Render dashboard.

## 6. Every morning
Open the dashboard -> **Login & connections** -> Upstox (and Zerodha) -> open the login page, sign in, and paste the
URL you land back on (it is your Render URL with `?code=...`). The token is stored in MongoDB.

## What to know
- **No password.** Anyone who has the URL can open the dashboard and press Start/Stop. Do not share it publicly.
- **Settings** you save in the dashboard are copied to MongoDB and restored when the service restarts.
  To change the defaults for everyone, edit `config/settings.yaml` in the repo and redeploy.
- **Logs** on the Logs page are lost on every restart. Render's own *Logs* tab shows live output.
- **Restarts**: Render restarts free services now and then, and on each deploy. The paper bot restarts by itself
  (`AUTO_START_BOT=paper`) and warms up again in about a minute. A running paper combo stays in MongoDB.
- **Memory** is 512 MB on the free plan. If the service restarts with "out of memory", lower `bot.warmup_bars`
  (for example 3000) in Settings.
- **Live trading** belongs on a VPS with a static IP registered with the brokers.
