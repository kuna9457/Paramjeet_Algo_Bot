# Run the Nexus SMC bot in Docker (client PC)

The image holds the code only. **Keys/passwords are never inside it**; they come from a `.env` file on the client PC.

## A. On YOUR PC - build and ship the image

    cd nexus_smc_bot
    docker build -t nexus-smc-bot:latest .

Pick ONE way to get it to the client:

**Option 1 - Docker Hub (private repo, free for 1 private repo)**

    docker login
    docker tag nexus-smc-bot:latest YOURHUBUSER/nexus-smc-bot:1.0
    docker push YOURHUBUSER/nexus-smc-bot:1.0

Keep the repo PRIVATE. Give the client a read-only access token (Docker Hub -> Account -> Personal access tokens).

**Option 2 - a file (no account needed)**

    docker save nexus-smc-bot:latest | gzip > nexus-smc-bot.tar.gz     (Git Bash)
    # or in PowerShell:  docker save -o nexus-smc-bot.tar nexus-smc-bot:latest

Send the file (Drive/WeTransfer/USB). Client loads it with `docker load -i nexus-smc-bot.tar.gz`.

## B. On the CLIENT PC - one-time setup

1. Install **Docker Desktop** (Windows). In its settings tick *Start Docker Desktop when you sign in*.
2. Make a folder, e.g. `C:\nexus-bot`, and put 2 files in it:
   - `docker-compose.yml` (from this project)
   - `.env` (copy `.env.example`, fill in MONGO_URI, broker keys, Telegram, and **DASHBOARD_PASSWORD**)
3. If you used Docker Hub: run `docker login` once, and edit the `image:` line in docker-compose.yml to
   `image: YOURHUBUSER/nexus-smc-bot:1.0` (remove the `build: .` line too). If you used the file: `docker load -i ...` first;
   the default image name `nexus-smc-bot:latest` then works as-is.
4. Start:

       docker compose up -d

5. Open **http://localhost:8501**, log in with the dashboard password.

The container restarts by itself after a reboot or crash (`restart: unless-stopped`).

## C. Daily use
- Every morning: Dashboard -> *Login & connections* -> Upstox/Zerodha login (tokens are saved in MongoDB).
- `AUTO_START_BOT` (set in `.env`): `off` (default, press Start in dashboard) | `paper` | `live`.
  `live` makes the bot start REAL trading each time the container restarts - use only when you are sure.
- Logs: dashboard Logs page, or `docker logs -f nexus-smc-bot`.
- Stop: `docker compose stop`   Start again: `docker compose start`
- Update to a new image: `docker compose pull && docker compose up -d` (or `docker load` the new file, then `docker compose up -d`).
  Logs, cache and dashboard-saved settings survive updates (Docker volumes).

## D. Before going LIVE - checklist
- [ ] Client PC set to never sleep/hibernate; Windows Update active hours set; wired internet if possible.
- [ ] Windows time sync ON.
- [ ] **Static IP**: Indian brokers (Upstox/Zerodha) now require orders from a registered static IP. A home connection
      normally has a changing IP, so live orders can be rejected. Ask the ISP for a static IP and register it
      in the broker developer console, or check what the broker currently allows.
- [ ] Redirect URL in the broker app matches `UPSTOX_REDIRECT_URI` / Kite redirect.
- [ ] Run **paper** for a few days first, then live with the smallest lot size.
- [ ] Set `DASHBOARD_PASSWORD`. The dashboard is published on 127.0.0.1 only (this PC). Do not open it to the internet.
- [ ] Keep MongoDB Atlas reachable (free M0 is fine), or set `storage.backend: local` in Settings to use the Docker volume.
