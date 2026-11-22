# Set up on another computer, and migrate from one

The same repo runs on every machine with no fork and no hand-porting. The code is identical everywhere; only machine-local state differs: `config.json`, `.env`, a small data folder, and the switches under **Settings → Available features**.

The lists below were derived from the code, not from memory: every file the app opens, writes or connects to (searching `src/` and `app/` for file opens and writes, `sqlite3.connect`, `Path.home()` and environment reads), plus every file `.gitignore` keeps out of the repo. `tests/test_setup_doc.py` fails when a config key, an `.env.example` key or a data file named in the code is missing from this page.

## Set up a new machine

1. **Prerequisites.** Python 3.9 or newer on `PATH` (3.11 or newer for the local usage log, which switches itself off with a reason on an older interpreter), Git, and GitHub Copilot used in VS Code (any variant) and/or the Copilot CLI on that machine. Node is only needed to run the verification gate.
2. **Clone.**
   ```
   git clone https://github.com/ferraroroberto/github-copilot-usage.git
   cd github-copilot-usage
   ```
3. **Run.** `start.bat` (Windows) or `bash scripts/start.sh` (macOS / Linux) creates `.venv`, installs `requirements.txt` (fastapi, uvicorn, httpx), starts the server on `http://127.0.0.1:8377/` and opens it. For a manual setup: create a venv, `pip install -r requirements.txt`, then `python -m app`.
4. **Config (optional).** Nothing is required: every key has a default. `config.json` is created beside the app the first time a setting is saved in the dashboard. To start from the documented shape, copy `config.example.json` to `config.json` first. For the billing card or the Telegram pace alert, copy `.env.example` to `.env` and fill in the keys you want.
5. **Check it.** Open `/health` (it answers `ok` with the version), then the **Data sources** card on the Overview tab: it lists every editor folder and Copilot CLI source found on this machine and how many requests each contributed.
6. **Switch off what cannot run here.** Open **Settings → Available features**. A block whose dependency is missing is marked *Unavailable on this machine* with the reason under the list (no billing token, no Telegram settings, no Copilot CLI folder, a Python too old for the usage log). Switch it off and its part of the page disappears and its work stops. Every block is on by default, and the choice is stored in this machine's `config.json` under `features`.
7. **Tray (Windows, optional).** `tray.bat` runs the server under a tray icon; it creates `.venv` and installs the tray extras itself. Put a shortcut to it in `shell:startup` to start it on login.
8. **Update.** `git pull`, then restart: `tray.bat --restart` for the tray (it only touches this repo's own `.venv` process, found under either path spelling of a junctioned folder), or stop and rerun the foreground command. Open `/health` to confirm the new build: it carries `version` and `git_sha`, and `tray.bat` prints both once the server answers.

## Migrate from an existing machine

Copy only what is listed as needed. Nothing here is secret except `.env`: move it over a channel you trust and never commit it.

| What | Where | Needed? | Notes |
|---|---|---|---|
| `config.json` | repo root | optional | Holds `port`, `monthly_credits`, `cycle_reset_day`, `extra_roots`, `features`, `tailnet_access`, `model_aliases`, `model_colors`, `telemetry_start_date` and `session_names`. Without it every default applies. Edit it after copying, see below. |
| `.env` | repo root | optional | Only the key names are listed here: `GITHUB_COPILOT_BILLING_PAT` (billing card), `COPILOT_USAGE_TELEGRAM_BOT_TOKEN` and `COPILOT_USAGE_TELEGRAM_CHAT_ID` (pace alert), and, if you set them, `COPILOT_USAGE_PORT`, `GITHUB_COPILOT_USAGE_DATA_DIR` and `FLEET_DATA_ROOT`. Leave a key empty or out and its block reports itself unavailable. |
| `usage.sqlite3` | the app's data folder | optional, rebuildable | The usage log (views and taps, never content). Deleting it only resets that history. |
| `read_model.sqlite3` | the app's data folder | no, rebuildable | The parsed requests and the manifest of the files they came from. Never copy it: the first start on the new machine parses that machine's own logs. Deleting it only makes the next start parse everything again. |
| `pace-alert.json` | the app's data folder | optional | Remembers which billing cycle already sent its Telegram message. Copy it to avoid a repeat message in the same cycle; without it, one message may be sent again when the projection is already past the allowance. |
| `health_check.json` | the app's data folder | no, rebuildable | The last **Settings → Data sources → Health check** result (timings and statement counts, never a prompt or path). Deleting it only clears the last-result sentence until the next run. |
| `tailnet.crt`, `tailnet.key` | the app's data folder | no, rebuilt | The Tailscale certificate and its private key, written by `tailscale cert` only while `tailnet_access` is on. Never copy them: the next start fetches a certificate for the new machine's own name. |
| Copilot's own session logs | the editor's `User/workspaceStorage` and `globalStorage`, the CLI's `session-state` folder and `session-store.db` | not migrated | These are the data the dashboard reads, and it only reads them. Each machine shows its own. To count another machine's history, copy that editor's `User/workspaceStorage` folder to any folder here and add it under **Settings → Extra storage roots**; a CLI history has to sit in the same place on this machine to be read. |
| `.venv` | repo root | no, rebuilt | Never copy it: the first run creates it. |
| Caches | memory, `__pycache__`, `.pytest_cache`, `.launcher-tmp/` | no, rebuildable | Rebuilt on the first request or on **Force refresh**. |
| Theme, text size, full screen on Edge, last tab, folded cards | the browser's local storage | no | Per browser, set once on each device. |

The data folder is, in order of precedence: `GITHUB_COPILOT_USAGE_DATA_DIR`, then `FLEET_DATA_ROOT/github-copilot-usage/`, then the platform default described in the README's Privacy section. It is created on the first write, never inside the repo.

### Edit after copying

- **`extra_roots`:** these are paths on the old machine. Replace them with this machine's paths, or empty the list and let discovery find the editors.
- **`features`:** the old machine's switches. Review **Settings → Available features** here; a block that was on there may be unavailable on this machine, and one you want on a single machine only (the pace alert, so that one chat is not messaged twice) belongs off everywhere else.
- **`tailnet_access`:** a machine-specific choice. It reads this machine's own Tailscale address and name at start, so it works on the new machine only if that machine has the `tailscale` command, is connected, and has HTTPS Certificates enabled for its tailnet; otherwise the server stays on loopback and Settings says why. Leave it off unless you want phone access from that machine.
- **`port`:** change it if 8377 is taken here. `COPILOT_USAGE_PORT` overrides it.
- **`monthly_credits`, `cycle_reset_day`:** correct them if the account or plan differs.
- **`session_names`:** names are keyed by session id, so they only match when the same sessions exist here.
- **Hosts:** none to edit. The app binds to `127.0.0.1`, plus this machine's own Tailscale address while `tailnet_access` is on (never `0.0.0.0`); its names and addresses are read at start, never stored. Its only outbound calls are `api.github.com` (billing card) and `api.telegram.org` (pace alert), each only while its block is on and configured.

### Check the copy worked

1. `/health` answers `ok`.
2. `GET /api/config` returns the values you copied; `GET /api/features` lists every block with `enabled` and, where something is missing, `available: false` and the reason.
3. `GET /api/summary?period=all` and the **Data sources** card show this machine's own requests, not zeros, once Copilot has been used here.
4. After a restart the switches are as you left them: they live in `config.json`, not in the browser.
5. With `tailnet_access` on, `GET /api/tailnet` reports `live: true` and the HTTPS URL to open from the phone, or `live: false` with the reason it stayed on loopback.
