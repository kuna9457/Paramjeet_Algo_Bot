# Release guide (your side)

## Ship a new version
1. Make your code changes in `nexus_smc_bot/`.
2. Double-click `installer\Release_Admin.bat` (or `python installer\release_admin.py`).
3. Choose Patch / Minor / Major, write the release notes, keep "Run tests" and "Smoke-test" ticked, press **Build installer**.
   It: runs tests -> bumps `VERSION` -> bundles code + embedded Python + packages -> smoke-tests -> compiles Inno Setup
   -> writes the SHA-256 -> (optional) `git commit` + tag `vX.Y.Z` (+ push).
4. Press **Open GitHub release page**, create the release for tag `vX.Y.Z`, and upload BOTH files from `installer\dist\`:
   - `NexusBot_Setup_X.Y.Z.exe`
   - `NexusBot_Setup_X.Y.Z.exe.sha256`
   Paste the release notes into the description. Publish.

Command line equivalent: `python installer\build.py --bump patch --tests --git-commit`

## What the client does
- First time: run `NexusBot_Setup_X.Y.Z.exe` (no admin rights needed) -> Desktop icon "Nexus SMC Bot".
- The icon starts the dashboard and opens http://localhost:8501. Keys are typed in the dashboard:
  *Login & connections -> API keys (.env)* (saved in the install folder's `app\.env`, never inside the installer).
- Updates: every time the icon is opened the app checks the latest GitHub Release. If newer, it asks, downloads,
  verifies the checksum, installs and reopens. It never updates while the trading bot is running.

## What is kept on update
`.env`, `logs\`, `data_cache\`, `config\settings.yaml` (client's settings). Code and the Python runtime are replaced.
Uninstall also keeps these (delete `%LOCALAPPDATA%\Programs\NexusBot` by hand to remove everything).

## Private repo?
The update check calls the GitHub API. For a PRIVATE repo it needs a read-only token in `release_config.json`
(`"github_token"`), which ships inside the installer. Safer: create a separate PUBLIC repo that only holds releases
(installers, no source) and set `github_repo` to it. A private repo with no token just means "no update offered".

## Notes
- `config\settings.yaml` that is in the project at build time becomes the client's default on a fresh install.
- Python packages are cached in `installer\build\cache`; tick "Re-download Python packages" to refresh them
  (requirements.txt versions are `>=`, so a rebuild may pull newer package versions).
- Unsigned installers can show a Windows SmartScreen warning: "More info" -> "Run anyway".
- Tools needed on YOUR PC only: Python 3.11, Inno Setup 6 (`winget install JRSoftware.InnoSetup`), git.
