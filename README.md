# github-copilot-usage

A local, self-contained dashboard that shows you **exactly where your GitHub Copilot premium requests go** — per session, per request, per model, per project — using the detailed usage data Copilot already writes to your own machine.

If you have a limited Copilot plan and only ever see the counter go *up* with no idea why, this answers: which sessions burned the credits, which models cost what, how many tokens each request sent and received, and even **what your prompt tokens are actually made of** (spoiler: often ~50% system instructions and ~45% tool definitions — your own words can be as little as 3%).

Everything runs locally. Read-only. Nothing ever leaves your machine.

## Quick start

Requires Python 3.9+ and a machine where you use GitHub Copilot in VS Code (any variant) and/or the Copilot CLI.

```
git clone https://github.com/ferraroroberto/github-copilot-usage.git
cd github-copilot-usage
start.bat        # Windows
./start.sh       # macOS / Linux
```

That's it — the first run creates a virtual environment, installs three dependencies (fastapi, uvicorn, httpx), starts the server on `http://127.0.0.1:8377/` and opens the dashboard in your browser.

## What you get

- **Premium requests (credits), tokens in/out, request counts** for Today / Week / Month / **billing Cycle** / All, with vs-previous-period deltas.
- **Billing-cycle burn-down** — set your monthly credit allowance and see % used, a "where you should be today" pace marker, and a linear projection to cycle end. Get the right number from VS Code's own Copilot menu: it shows **"Credits X / Y used"** — enter Y (plan defaults are Business 300 / Enterprise 1000 / Pro 300 / Pro+ 1500, but org-provisioned pools can be much larger, e.g. 23,400).
- **Credits per model over time** (stacked daily/weekly/monthly chart) and tokens in/out trends.
- **Where your prompt tokens go** — VS Code records a per-request composition breakdown (System Instructions / Tool Definitions / Messages / attachments…). The dashboard aggregates it, weighted by prompt size. If "Tool Definitions" dominates, disabling unused tools and MCP servers shrinks *every single request* you make.
- **Session explorer** — every chat session, sortable by cost or recency; click one for the per-request drill-down: your prompt, mode (agent/ask/edit), resolved model, tokens in/out, **exact credits billed** (not an estimate — this is the number GitHub charges), and elapsed time.
- **By model / by project / by mode breakdowns** with credit shares.
- **CSV export** of every request for your own spreadsheets.
- **Official GitHub billing card** (optional): add a PAT and see the account-wide per-day per-model credit spend straight from GitHub's billing API — covers usage from *all* your devices, not just this machine.
- **Light + dark theme**, colorblind-validated chart palette, no CDN dependencies (Chart.js is vendored — works behind corporate proxies).

## Where the data comes from

All sources are **discovered automatically at runtime** and parsed read-only:

| Source | Location | Granularity |
|---|---|---|
| VS Code Copilot Chat | `<product-dir>/User/workspaceStorage/<hash>/chatSessions/*.jsonl` | per request — exact credits, tokens, model, prompt composition |
| VS Code chats without a workspace | `<product-dir>/User/globalStorage/emptyWindowChatSessions/` | per request |
| Copilot CLI (optional) | `~/.copilot/session-state/<uuid>/events.jsonl` | per session x model |
| GitHub billing API (optional) | `api.github.com` with your PAT | per day x model, account-wide |

`<product-dir>` is found by enumerating your platform's config root (`%APPDATA%` on Windows, `~/Library/Application Support` on macOS, `~/.config` on Linux) for anything that looks like a VS Code build — stock **Code**, **Code - Insiders**, **VSCodium**, **Cursor**, **Windsurf**, and any company-branded fork are all picked up without configuration. Remote-server installs (`~/.vscode-server`) are probed too. If your editor stores data somewhere exotic, add the path under **Settings → extra roots** (or `extra_roots` in `config.json`).

The **Data sources** card at the bottom of the dashboard shows exactly what was found and how many sessions/requests each source contributed — use it to confirm your setup is being read.

### Notes and honest limitations

- Copilot **code completions** (ghost text) are not in these logs — completions don't consume premium requests on paid plans, so the credit picture is still complete.
- Older chat sessions (before the extension started recording `copilotCredits` / token counts) exist as files but carry no billing data; they are listed as session files but contribute no requests. Your stats build up going forward.
- Session logs live on the machine (or remote host) where VS Code's extension host runs. For Remote-SSH / devcontainer work, run this tool where the sessions are, or point an extra root at the storage path.
- Local data covers **this machine only**. The optional billing card is the account-wide truth.
- 1 credit = 1 premium-request unit = $0.01 under GitHub's AI-credits billing model; the USD figures are derived from that.

## Configuration

`config.json` (created next to the app on first save; see `config.example.json`):

| Key | Default | Meaning |
|---|---|---|
| `port` | `8377` | dashboard port (loopback only), also `COPILOT_USAGE_PORT` env |
| `monthly_credits` | `300` | your monthly credit allowance (the "/ Y" total in VS Code's Copilot menu) |
| `cycle_reset_day` | `1` | day of month your billing cycle resets |
| `extra_roots` | `[]` | additional editor storage paths to scan |
| `include_copilot_cli` | `true` | also parse Copilot CLI sessions |

Everything except the port is editable live from the ⚙ Settings dialog.

### Optional: official billing card

Create a **fine-grained PAT** at <https://github.com/settings/personal-access-tokens> with the **"Plan" read-only** account permission, then:

```
cp .env.example .env     # and paste the token into GITHUB_COPILOT_BILLING_PAT
```

No PAT → the card simply explains it's not configured. The token is only ever sent to `api.github.com`.

Note: this is the **user** billing endpoint, so it works for individually-paid plans (Pro / Pro+). On a **Business / Enterprise seat the organization pays**, and this endpoint typically has no data for you — there, the local session stats plus the quota bar in VS Code's Copilot menu are your sources of truth.

## Optional: system tray (Windows)

`tray.bat` runs the server under a tray icon (Open dashboard / Restart / Quit) instead of a console window — handy for keeping it always on. It works directly on a fresh clone (it creates the venv and installs everything itself, including two small tray extras: pystray, pillow), so it is a full alternative to `start.bat`. Re-running it is a no-op while the tray lives; `tray.bat --restart` restarts it. Detection is scoped to this repo's `.venv`, so no other Python process is ever touched. Put a shortcut in `shell:startup` to launch on login.

## API

The dashboard is a thin client over a JSON API you can script against (interactive docs at `/api/docs`):

```
GET /health
GET /api/summary?period=today|week|month|cycle|all
GET /api/sessions?period=...
GET /api/sessions/{session_id}
GET /api/export.csv?period=...
GET /api/billing
GET /api/config          POST /api/config
```

## Development

```
.venv/Scripts/python.exe -m pytest      # tests (synthetic session fixtures)
.venv/Scripts/python.exe -m app         # run without opening a browser
```

Layout: `app/` (FastAPI server, discovery, the jsonl replay parser, aggregation, optional billing client, optional tray) · `static/` (vanilla-JS dashboard, vendored Chart.js) · `tests/`.

## Privacy

Read-only over Copilot's own local log files; zero subprocesses; binds to `127.0.0.1` only; no telemetry; the only outbound call is the optional GitHub billing API when *you* configure a PAT. Your prompts stay on your machine — the dashboard shows the first ~200 characters of each request message so you can recognize what a request was, and the CSV export contains the same.

## License

MIT — see [LICENSE](LICENSE). Share it with your team.
