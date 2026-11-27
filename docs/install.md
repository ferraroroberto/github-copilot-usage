# Install, update and move github-copilot-usage

github-copilot-usage moves to another Windows PC the way any app in the toolbox does: clone it and run one `setup.bat`. An update is a pull plus a restart. Neither ever resets or overwrites what a PC has configured.

The code is identical on every machine; only machine-local state differs: `config.json`, `.env`, a small data folder outside the repo, and the switches under **Settings → Available features**. Everything the app reads, writes or connects to is listed here, and `tests/test_setup_doc.py` fails when a config key, an `.env.example` key or a data file named in the code is missing from this page.

## What you need

- Windows 10 or 11 and `git`.
- Python 3.11 or newer for `setup.bat` (the `py` launcher, or `python` on `PATH`): it reads `.fleet.toml`, which needs `tomllib`. The app itself runs on Python 3.9 or newer, and the local usage log needs 3.11. On an older interpreter skip `setup.bat` and use `scripts\start.bat`, which still creates `.venv` and starts the dashboard (macOS and Linux: `bash scripts/start.sh`).
- GitHub Copilot used in VS Code (any variant) and/or the Copilot CLI on that PC: those session logs are the data the dashboard reads, and it only reads them.
- Optional: the `tailscale` command, only to open the dashboard from a phone. Node is only needed to run the verification gate.

## Fresh PC

```powershell
git clone https://github.com/ferraroroberto/github-copilot-usage.git
cd github-copilot-usage
.\setup.bat
.\tray.bat
```

`setup.bat` runs `src/installer.py`, which:

1. creates `.venv` when it is missing;
2. installs `requirements.txt` (fastapi, uvicorn, httpx), and later reinstalls only when that file changed (its hash is kept inside `.venv`);
3. creates `config.json` from `config.example.json` and `.env` from `.env.example` when they are missing;
4. ends with two lists: the required items, each `ok`, `missing` or `unknown`, and the optional features, each `available`, `off` or `unknown` with what it unlocks.

Nothing is required beyond that: every config key has a default, and the dashboard opens at `http://127.0.0.1:8377/`. `.\tray.bat` runs it under a tray icon (it installs the two tray extras, pystray and pillow, on its first run); `.venv\Scripts\python.exe -m app --open` runs it in the foreground and opens the browser.

Setup has succeeded when it ends with `Setup complete` and exits 0; an optional feature that is `off` never changes that. The optional features and the rest that may want you:

- **Copilot session logs:** `off` when no editor folder or Copilot CLI folder is found here. Use Copilot on this PC, or add another PC's `workspaceStorage` folder under **Settings → Extra storage roots**.
- **Tailscale CLI:** `off` is fine unless you want phone access.
- **`.env` keys:** the file is created with every key empty. Fill `GITHUB_COPILOT_BILLING_PAT` for the official billing card, and `COPILOT_USAGE_TELEGRAM_BOT_TOKEN` and `COPILOT_USAGE_TELEGRAM_CHAT_ID` for the pace alert. A block with its key empty reports itself unavailable, with the reason, under **Settings → Available features**; switch it off there and its part of the page disappears.
- **Start at log on:** put a shortcut to `tray.bat` in `shell:startup`. `setup.bat -Startup` has nothing to register for this app and says so.

`.\setup.bat -WhatIf` lists every change it would make and makes none.

Open `/health` to confirm the build: it answers `ok` and carries `version` and `git_sha`. The **Data sources** card on the Overview tab lists every editor folder and Copilot CLI source found on this PC and how many requests each contributed.

## Update

```powershell
git pull --ff-only
.\setup.bat
.\tray.bat --restart
```

No tracked file is rewritten at runtime, so the pull always fast-forwards. `tray.bat --restart` only touches this repo's own `.venv` process (found under either path spelling of a junctioned folder); for a foreground run, stop it and start it again. Confirm the new build on `/health`: `git_sha` changes.

`setup.bat` on a configured PC is safe, and it is how a new setting arrives:

- `config.json` is a settings file: an existing value is never changed. A key `config.example.json` has and the file lacks is added, after a timestamped backup beside it (`config.json.bak-<YYYYmmdd-HHMMSS>`), and the added key names are printed, never values. A file that does not parse is left alone and reported, and the run exits 1.
- `.env` is the PC's own list of secrets: created when missing, never rewritten.
- A second run reports `unchanged` and writes nothing.

## Migrate from an existing install

Use this on a PC that already runs an older copy of the dashboard: one that may not be a git clone, and may live at another path. The old copy keeps running until the new one is verified, and it is deleted only when you say so.

1. **Clone fresh beside it.** `git clone https://github.com/ferraroroberto/github-copilot-usage.git <new folder>` next to the old folder; never clone over it.
2. **Copy this PC's own files across.** From the old folder into the new one, keeping the relative path, copy only these. They are all ignored by git, so nothing tracked is touched, and nothing else needs to move:

   | What | Where | Carry over? | Notes |
   |---|---|---|---|
   | `config.json` | repo root | yes, if the old copy has one | Holds `port`, `monthly_credits`, `cycle_reset_day`, `extra_roots`, `features`, `tailnet_access`, `model_aliases`, `model_colors`, `telemetry_start_date` and `session_names`. Without it every default applies. Edit it after copying, see below. |
   | `.env` | repo root | yes, if the old copy has one | Secrets: move it over a channel you trust and never commit it. Key names only: `GITHUB_COPILOT_BILLING_PAT` (billing card), `COPILOT_USAGE_TELEGRAM_BOT_TOKEN` and `COPILOT_USAGE_TELEGRAM_CHAT_ID` (pace alert), and, if you set them, `COPILOT_USAGE_PORT`, `GITHUB_COPILOT_USAGE_DATA_DIR` and `FLEET_DATA_ROOT`. |
   | `usage.sqlite3` | the app's data folder | optional | The usage log (views and taps, never content). Skipping it only resets that history. |
   | `pace-alert.json` | the app's data folder | optional | Remembers which billing cycle already sent its Telegram message. Copy it to avoid a repeat message in the same cycle; without it, one message may be sent again when the projection is already past the allowance. |
   | `read_model.sqlite3` | the app's data folder | no, rebuildable | The parsed requests. The first start parses this PC's own logs. |
   | `health_check.json` | the app's data folder | no, rebuildable | The last **Settings → Data sources → Health check** result. |
   | `tailnet.crt`, `tailnet.key` | the app's data folder | no, rebuilt | The Tailscale certificate and its private key, written by `tailscale cert` only while `tailnet_access` is on. The next start fetches a certificate for this PC's own name; never copy them. |
   | `.venv`, `__pycache__`, `.pytest_cache`, `.launcher-tmp/` | repo root | no | `.venv` is created by `setup.bat`; the rest are caches. |

   The data folder is, in order of precedence: `GITHUB_COPILOT_USAGE_DATA_DIR`, then `FLEET_DATA_ROOT/github-copilot-usage/`, then `C:\sqlite\github-copilot-usage\` on Windows. It lives outside the repo, so on the same PC the old and the new copy share it and there is nothing to copy; on another PC copy the two optional files into that folder there (it is created on the first write). Copilot's own session logs are never migrated: each PC shows its own.
3. **Top them up.** Run `.\setup.bat -WhatIf` in the new folder and read what it would change, then `.\setup.bat`. `config.json` gains only the keys it lacks, after a backup; the values you copied stay as they were.
4. **Edit what was PC-specific** in the copied `config.json`:
   - `extra_roots`: paths on the old PC. Replace them with this PC's paths, or empty the list and let discovery find the editors.
   - `features`: the old PC's switches. Review **Settings → Available features** here: a block that was on there may be unavailable on this PC, and one you want on a single PC only (the pace alert, so one chat is not messaged twice) belongs off everywhere else.
   - `tailnet_access`: this PC's own choice. It works only if this PC has the `tailscale` command, is connected and has HTTPS Certificates enabled for its tailnet; otherwise the server stays on loopback and Settings says why. Leave it off unless you want phone access from here.
   - `port`: change it if 8377 is taken here; `COPILOT_USAGE_PORT` overrides it.
   - `monthly_credits`, `cycle_reset_day`: correct them if the account or plan differs.
   - `session_names`: names are keyed by session id, so they only match when the same sessions exist here.
5. **Stop the old copy.** Quit its tray icon from the menu, or stop its foreground console, and delete any `shell:startup` shortcut that points at the old folder. Two copies cannot both serve the same port.
6. **Start the new one.** `.\tray.bat` in the new folder, and a `shell:startup` shortcut to it if it should start at log on.
7. **Verify it.**
   - `/health` answers `ok` with the new `git_sha`.
   - `GET /api/config` returns the values you copied, and `GET /api/features` lists every block with `enabled` and, where something is missing, `available: false` and the reason.
   - `GET /api/summary?period=all` and the **Data sources** card show this PC's own requests, not zeros, once Copilot has been used here.
   - With `tailnet_access` on, `GET /api/tailnet` reports `live: true` and the HTTPS URL to open from the phone, or `live: false` with the reason it stayed on loopback.

   If anything is wrong, stop the new copy and start the old one again: it is untouched.
8. **Retire the old folder, only after you confirm.** Nothing in this playbook or in `setup.bat` deletes the old copy. When you are satisfied, rename it first (for example `github-copilot-usage.retired`), run on the new copy for a while, and delete it yourself once you are sure. An agent working through this playbook asks you before it deletes anything.

## Uninstall

1. Turn off log-on start: delete the `tray.bat` shortcut from `shell:startup`.
2. Quit the tray from its menu (or stop the foreground console).
3. Delete the repo folder. The app registered nothing outside it except its data folder and, if `tailnet_access` was on, a certificate in that folder.
4. Delete the data folder (`C:\sqlite\github-copilot-usage\` unless you moved it) if you do not want its usage history. Copilot's own logs are never touched.

Keep a copy of `config.json`, `.env` and `usage.sqlite3` first if you may want them back.
