# Install, update and migrate github-copilot-usage

github-copilot-usage installs, updates and migrates the way any app in the toolbox does: clone it and run one `setup.bat`. An update is a pull plus a restart. A migration, onto a fresh clone on the same PC or on another one, is one `setup.bat` call. None of them ever resets or overwrites what a PC has configured.

The code is identical on every machine; only machine-local state differs: `config.json`, `.env`, a small data folder outside the repo, and the switches under **Settings → Available features**. Everything the app reads, writes or connects to is listed here, and `tests/test_setup_doc.py` fails when a config key, an `.env.example` key or a data file named in the code is missing from this page.

## Install

You need:

- Windows 10 or 11 and `git`.
- Python 3.11 or newer for `setup.bat` (the `py` launcher, or `python` on `PATH`): it reads `.fleet.toml`, which needs `tomllib`. The app itself runs on Python 3.9 or newer, and the local usage log needs 3.11. On an older interpreter skip `setup.bat` and use `scripts\start.bat`, which still creates `.venv` and starts the dashboard (macOS and Linux: `bash scripts/start.sh`).
- GitHub Copilot used in VS Code (any variant) and/or the Copilot CLI on that PC: those session logs are the data the dashboard reads, and it only reads them.
- Optional: the `tailscale` command, only to open the dashboard from a phone. Node is only needed to run the verification gate.

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

### Uninstall

1. Turn off log-on start: delete the `tray.bat` shortcut from `shell:startup`.
2. Quit the tray from its menu (or stop the foreground console).
3. Delete the repo folder. The app registered nothing outside it except its data folder and, if `tailnet_access` was on, a certificate in that folder.
4. Delete the data folder (`C:\sqlite\github-copilot-usage\` unless you moved it) if you do not want its usage history. Copilot's own logs are never touched.

Keep a copy of the files listed under [Migrate](#migrate) first if you may want them back.

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

## Migrate

What moves is declared in `[install.migrate]` of `.fleet.toml`: `config.json` and `.env`, with `.env` compared by hash as a secret. The data folder stays where it is and is shared. The installer does the whole move: it stops the old copy, copies those two files (never over one already there), installs, starts the app with `tray.bat --restart` and verifies it. Nothing in it or in `setup.bat` deletes the old copy.

**Reinstall in the same folder (the usual case).** From a terminal whose current folder is outside the app. The folder's own `setup.bat` must already know `-InPlace`; run `git pull --ff-only` in it first if it does not.

```powershell
cd <parent folder>
.\github-copilot-usage\setup.bat -InPlace -WhatIf    # read the backup name and every step; nothing changes
.\github-copilot-usage\setup.bat -InPlace
```

It keeps the replaced copy as `github-copilot-usage.bak-YYYYMMDD-N` and passes only when the new clone serves its own commit on `/api/version`. The path is unchanged, so a `shell:startup` shortcut needs no edit.

**Beside it (another folder, or an old copy that is not a git clone):**

```powershell
git clone https://github.com/ferraroroberto/github-copilot-usage.git <new folder>
cd <new folder>
.\setup.bat -MigrateFrom <old folder> -StopOld -WhatIf
.\setup.bat -MigrateFrom <old folder> -StopOld
```

`-StopOld` is needed on the same PC: both copies would bind the same port.

Verify asks the port set in `config.json`, else 8377. A port set only through `COPILOT_USAGE_PORT` is not read by the installer, so verify would ask the wrong one: put it in `config.json` too. On an interpreter older than 3.11 the installer cannot run: copy `config.json` and `.env` by hand into a fresh clone and start it.

### What moves and what stays

| What | Where | Moved by the installer? | Notes |
|---|---|---|---|
| `config.json` | repo root | yes | Holds `port`, `monthly_credits`, `cycle_reset_day`, `extra_roots`, `features`, `tailnet_access`, `model_aliases`, `model_colors`, `telemetry_start_date` and `session_names`. Without it every default applies. A value that names the old folder is rewritten to the new one. |
| `.env` | repo root | yes | Secrets. Key names only: `GITHUB_COPILOT_BILLING_PAT` (billing card), `COPILOT_USAGE_TELEGRAM_BOT_TOKEN` and `COPILOT_USAGE_TELEGRAM_CHAT_ID` (pace alert), and, if you set them, `COPILOT_USAGE_PORT`, `GITHUB_COPILOT_USAGE_DATA_DIR` and `FLEET_DATA_ROOT`. |
| `usage.sqlite3` | data folder | no, shared | The usage log (views and taps, never content). |
| `pace-alert.json` | data folder | no, shared | Remembers which billing cycle already sent its Telegram message. |
| `read_model.sqlite3` | data folder | no, shared and rebuildable | The parsed requests. |
| `health_check.json` | data folder | no, shared and rebuildable | The last **Settings → Data sources → Health check** result. |
| `tailnet.crt`, `tailnet.key` | data folder | no, shared and rebuilt | The Tailscale certificate and its private key, written by `tailscale cert` only while `tailnet_access` is on. |
| `.venv`, `__pycache__`, `.pytest_cache`, `.launcher-tmp/` | repo root | no | `.venv` is created by `setup.bat`; the rest are caches. |

The data folder is, in order of precedence: `GITHUB_COPILOT_USAGE_DATA_DIR`, then `FLEET_DATA_ROOT/github-copilot-usage/`, then `C:\sqlite\github-copilot-usage\` on Windows. It lives outside the repo, so on the same PC the old and the new copy share it and there is nothing to copy. Copilot's own session logs are never migrated: each PC shows its own.

### Only you can do these

- **Moving to another PC:** copy the old `config.json` and `.env` into a folder on the new one, over a channel you trust, and pass that folder to `-MigrateFrom` (no `-StopOld`: nothing runs from it). Optionally copy `usage.sqlite3` (the usage history) and `pace-alert.json` (to avoid a repeat message in the same cycle) into the new PC's data folder. Never copy `tailnet.crt` or `tailnet.key`: the next start fetches a certificate for that PC's own name.
- **On another PC, edit what was PC-specific** in `config.json`, then run `.\tray.bat --restart`:
  - `extra_roots`: paths on the old PC. Replace them with this PC's paths, or empty the list and let discovery find the editors.
  - `features`: review **Settings → Available features**. A block that was on there may be unavailable here, and one you want on a single PC only (the pace alert, so one chat is not messaged twice) belongs off everywhere else.
  - `tailnet_access`: works only if this PC has the `tailscale` command, is connected and has HTTPS Certificates enabled for its tailnet; otherwise the server stays on loopback and Settings says why.
  - `port`: change it if 8377 is taken here.
  - `monthly_credits`, `cycle_reset_day`: correct them if the account or plan differs.
  - `session_names`: names are keyed by session id, so they only match when the same sessions exist here.
- **Repoint** whatever stores the old path after a `-MigrateFrom` into another folder, such as the `shell:startup` shortcut: the run lists what still points at it.
- **Check the data** once verify passes: `GET /api/summary?period=all` and the **Data sources** card show this PC's own requests once Copilot has been used here, and with `tailnet_access` on `GET /api/tailnet` reports `live: true`, or `live: false` with the reason.
- **Fall back** if verify fails (the report names the step): stop the new copy, rename the new folder away and the backup or old folder back to its name, and start it; it is untouched.
- **Retire the backup** or the old folder once you are sure the new copy works, by deleting it yourself. An agent working through this playbook asks you before it deletes anything.
