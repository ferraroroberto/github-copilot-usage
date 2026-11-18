# github-copilot-usage

A local, self-contained dashboard that shows you **exactly where your GitHub Copilot premium requests go** — per session, per request, per model, per project — using the detailed usage data Copilot already writes to your own machine.

If you have a limited Copilot plan and only ever see the counter go *up* with no idea why, this answers: which sessions burned the credits, which models cost what, how many tokens each request sent and received, and even **what your prompt tokens are actually made of** (spoiler: often ~50% system instructions and ~45% tool definitions — your own words can be as little as 3%).

Everything runs locally. Read-only. Nothing ever leaves your machine.

## Quick start

Requires Python 3.9+ (the local usage log below needs 3.11+ and switches itself off, with a logged warning, on older interpreters) and a machine where you use GitHub Copilot in VS Code (any variant) and/or the Copilot CLI.

```
git clone https://github.com/ferraroroberto/github-copilot-usage.git
cd github-copilot-usage
start.bat        # Windows
bash scripts/start.sh   # macOS / Linux
```

That's it — the first run creates a virtual environment, installs three dependencies (fastapi, uvicorn, httpx), starts the server on `http://127.0.0.1:8377/` and opens the dashboard in your browser. Setting it up on another computer, or moving your config across: [docs/setup-and-migration.md](docs/setup-and-migration.md).

## What you get

- **Premium requests (credits), tokens in/out, request counts** for Today / Week / Month / **billing Cycle** / All, with vs-previous-period deltas.
- **Billing-cycle burn-down** — set your monthly credit allowance and see % used, a "where you should be today" pace marker, and a linear projection to cycle end. The meter and a level chip turn to attention at 80% used, to a warning when the projection passes the allowance (from day 3) and to critical at 100%; attention also appears as an Insights finding. Get the right number from VS Code's own Copilot menu: it shows **"Credits X / Y used"** — enter Y (plan defaults are Business 300 / Enterprise 1000 / Pro 300 / Pro+ 1500, but org-provisioned pools can be much larger, e.g. 23,400).
- **Credits per model over time** (stacked daily/weekly/monthly chart), plus tokens in/out, tokens-per-credit and cache read/write trends (a bucket that billed nothing or recorded no cache count is a gap, never a zero). *Older* and *Newer* step the chart through earlier windows (14 days, 12 weeks or 12 months at a time) and *Latest* returns; tapping a bar focuses the whole dashboard on that bucket's dates (the totals, sessions, request list, insights and CSV export follow it, and *Back* undoes it).
- **Custom session names** — rename a session from its detail sheet; the name replaces the title in every list, search and the CSV export, and *Restore title* brings the parsed one back. Names live in `config.json` (`session_names`), never in Copilot's files.
- **Telemetry start date** — *Settings → Data sources* takes a first day to count; earlier records are left out of every total. A period the date only partly covers is labelled *partial coverage*, one it does not reach shows `—` rather than 0, and a comparison against a span the date cuts into is left out.
- **Collapsible sections** — each card has a chevron that folds it away; the choice is remembered per browser.
- **Where your prompt tokens go** — VS Code records a per-request composition breakdown (System Instructions / Tool Definitions / Messages / attachments…). The dashboard aggregates it, weighted by prompt size. If "Tool Definitions" dominates, disabling unused tools and MCP servers shrinks *every single request* you make.
- **Session explorer** — every chat session, sortable by cost or recency from the Filters & sort sheet (the trigger beside the period control counts what differs from the defaults); click one for the per-request drill-down, a list of requests that each open in place: the requested and resolved model, mode, input volume, context processed, uncached input, cache read and write, output, **exact credits billed** (not an estimate — this is the number GitHub charges), elapsed time and your stored prompt in full (VS Code's log keeps the first 200 characters, the Copilot CLI the whole prompt). Under the diagnostics, a closed list gives the prompt size of each request with its change from the one before. CLI rows spread from session totals are marked `~` as estimates and a figure the CLI never recorded is a dash.
- **Request explorer** — the *Search requests* card opens every request of a period in one list: filter by project, model, mode, editor, result and credit range, search the prompt, the session (its custom name or the title its log carried), the project, models, mode, editor, source and request id, sort by time, credits, tokens or time taken, and page through. The option lists carry counts that follow the period and the other filters. The filters live in the URL (`/?view=requests&model=…`), so a filtered view can be bookmarked and survives a reload; each committed change (a filter, sort, page or period) is a browser history entry, so Back and Forward step through the filtered views and Back from the open list closes it (the dashboard's own period or focused range rides the URL the same way, as `?dash=…`). A row opens its session. A credit range matches billed requests only; unbilled requests stay listed when no range is set.
- **By model / by project / by mode breakdowns** with credit shares. Spellings of one model that differ only in case, spacing, separators (space, `_`, `-`) or a trailing release date (`claude-sonnet-4-20250514`) share one row; different versions and variants (`gpt-5`, `gpt-5.1`, `gpt-5-mini`) never merge. The row's tooltip lists every spelling it holds and each request keeps its raw label.
- **Model merging and colours** — *Settings → Models* lists the models in your local history. Merge one into another by exact name (`model_aliases`, no patterns), and recolour a model by provider family and opacity (`model_colors`); clearing a choice restores the default. Chart colours are the provider's brand colour (Google blue, Anthropic orange, OpenAI green, Microsoft amber, xAI ink, other gray); inside a provider the most used model is solid and the next ones fade to 80, 60, 40 and 20 % opacity (20 % is the floor), and a saved override wins. The legend, tooltips and the table name every series.
- **Context drill-down** — select a row of *Where your prompt tokens go* (or *Who drives this?*) to see the projects, models, sessions and requests carrying that part of the prompt, each with its share and a coverage line saying how much of the period has a composition at all (Copilot CLI and older logs have none). Figures are prompt size times the logged percentage: estimates, never per-tool billing. The sheet also lists the twenty files, tools and attachments that rode along on the most requests, and an opened request on the session sheet shows its own names; the log's variable data gives names only, so no file or tool content is kept, each name is cut at 160 characters, and a composition entry whose percentage is not a number from 0 to 100 is ignored.
- **Session diagnostics** — a session row and its detail flag a large prompt, a prompt that keeps growing and a high share of system and tool overhead. They are labelled heuristics, with the thresholds as constants in `src/session_diagnostics.py`. The prompt-size flags use fixed token cuts; the overhead flag is relative to your own sessions (the top tenth, once at least ten sessions carry a composition), because system and tool overhead is high on almost every session and its level differs per machine.
- **Insights** — a card of local, rule-based findings (top contributors, change against the previous period, billing-cycle risk, prompt composition), each with a severity and a tap that opens the session, drill-down or filtered request list it refers to. A period with no or too few requests says so rather than guessing. No model call, nothing leaves the machine.
- **Read model** — parsed requests are kept in `read_model.sqlite3` in the app's data folder (see Privacy), with one manifest row per source file. A refresh or a restart parses only the chat logs that are new or changed and drops the records of logs that are gone, so a restart with unchanged logs parses nothing. A corrupt file, or one from another version, is moved aside to `read_model.sqlite3.bad` and rebuilt; if the folder cannot be written the dashboard parses in memory and the refresh reply says why. The request explorer and the session list are answered by SQL over this file, read from the same scan the dashboard's other figures came from; when the file cannot answer (it is in memory, or a newer scan has replaced it) they are computed from the parsed records instead, with the same result. *Settings → Data sources → Health check* backs the read model up into a temporary folder and times the real API over that copy (summary cold and warm, each period, sessions, requests with filters and paging, session detail, refresh, a safe settings save, cache clear) against a 1.5 s budget, three samples each; the live read model, your settings and Copilot's files are never written. The result, timings and SQL statement counts only, is kept in the data folder.
- **Refresh and clear cache** — the page re-reads every 30 s, and a tab that was hidden longer than that reloads once the moment it is visible again. The header's freshness readout opens a menu with *Refresh* (re-read what changed) and *Force refresh* (drop every parsed record, the read model included, and rebuild from the files); *Settings → Data sources → Clear cache* drops them and rebuilds on the next load. Each reports its result in a toast. Overlapping refreshes share one rebuild, which keeps running if the page that asked for it goes away, and a summary is recomputed whenever any request's content changes.
- **Available features** — *Settings → Available features* switches whole parts of the app on or off for this machine: Copilot CLI sessions, the official billing card, the Telegram pace alert and the usage log. All are on by default; a block that is off hides its part of the page and stops its work, with no restart, and one whose dependency is missing says why. The switches are stored in `config.json`, so the code is the same everywhere.
- **CSV export** of every request for your own spreadsheets.
- **Official GitHub billing card** (optional): add a PAT and see the account-wide per-day per-model credit spend straight from GitHub's billing API — covers usage from *all* your devices, not just this machine.
- **Light + dark theme** and a **Small / Default / Large text-size** setting (Settings dialog), colorblind-validated chart palette, no CDN dependencies (Chart.js is vendored — works behind corporate proxies).
- **Installable as a PWA** — add it to your home screen/dock from the browser for an app-like icon and window; the icon set is generated locally by `scripts/gen_icons.py`, no external branding service involved.

## Where the data comes from

All sources are **discovered automatically at runtime** and parsed read-only:

| Source | Location | Granularity |
|---|---|---|
| VS Code Copilot Chat | `<product-dir>/User/workspaceStorage/<hash>/chatSessions/*.jsonl` | per request — exact credits, tokens, model, prompt composition |
| VS Code chats without a workspace | `<product-dir>/User/globalStorage/emptyWindowChatSessions/` | per request |
| Copilot CLI (optional) | `~/.copilot/session-store.db` (`assistant_usage_events`) | per prompt x model — exact, active sessions and sub-agents included |
| Copilot CLI fallback | `~/.copilot/session-state/<uuid>/events.jsonl` | session x model totals, spread over the session's prompts and marked estimated |
| GitHub billing API (optional) | `api.github.com` with your PAT | per day x model, account-wide |

`<product-dir>` is found by enumerating your platform's config root (`%APPDATA%` on Windows, `~/Library/Application Support` on macOS, `~/.config` on Linux) for anything that looks like a VS Code build — stock **Code**, **Code - Insiders**, **VSCodium**, **Cursor**, **Windsurf**, and any company-branded fork are all picked up without configuration. Remote-server installs (`~/.vscode-server`) are probed too. If your editor stores data somewhere exotic, add the path under **Settings → extra roots** (or `extra_roots` in `config.json`).

The **Data sources** card at the bottom of the dashboard shows exactly what was found and how many sessions/requests each source contributed — use it to confirm your setup is being read. A VS Code chat log reachable twice (a profile copied to another editor folder, a restored backup, an extra root inside a discovered one) counts once: requests with the same editor, session id and request id collapse to the copy in the newest file (the smallest path on a tie), and the card says how many duplicates it ignored. A request missing any of those ids is never merged.

The CLI database's schema is undocumented: when it is missing or has an unexpected shape, the CLI falls back to `events.jsonl` and the Data sources card says why. Every request carries `usage_source` (`vscode`, `session-store`, `events.jsonl`, `events.jsonl-allocated`, `events.jsonl-checkpoint`, `unavailable`) and `usage_estimated`. Estimated values come from session or checkpoint totals spread across prompts (each checkpoint's credit delta, and the shutdown remainder after the last one, is split over the prompts in its interval by their assistant output tokens, evenly without that hint); the UI marks them `~`. A CLI request whose log carries no input or output token counts has `null` tokens and the source `unavailable`: the dashboard shows `—` for it rather than 0, and totals say how many requests had no token data.

### Token definitions

Credits stay the billing measure. Cache tokens are kept in their own buckets, and these definitions apply on every card, chart, breakdown and detail view:

| Figure | Definition |
|---|---|
| Tokens in (input volume, `prompt_tokens`) | CLI: uncached input + cache-write; the store's exact uncached input (`token_details_json`) when a row has it, else total input less the cache buckets, never below 0. VS Code: `promptTokens` |
| Cache read / cache write | separate buckets; cache-read never counts toward input volume or output |
| Total input (context processed) | CLI total input including cache-read |
| Effective input | uncached input + 10% of cache-read |
| Effective output | output + 10% of cache-write |
| Tokens per credit | (effective input + effective output) / billed credits; null when nothing was billed or a token count was not recorded |
| Unknown tokens | a count the source did not record is `null`, never 0: totals add the recorded ones, and are `null` when no request recorded that count |

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
| `port` | `8377` | dashboard port (loopback, and the Tailscale address when `tailnet_access` is on), also `COPILOT_USAGE_PORT` env |
| `tailnet_access` | `false` | also serve the dashboard over HTTPS on this machine's Tailscale address, read at server start; see [Open from your phone](#open-from-your-phone-tailscale) |
| `monthly_credits` | `300` | your monthly credit allowance (the "/ Y" total in VS Code's Copilot menu) |
| `cycle_reset_day` | `1` | day of month your billing cycle resets |
| `extra_roots` | `[]` | additional editor storage paths to scan |
| `features` | `{}` | feature blocks switched off on this machine, `{"<block>": false}`; `copilot_cli`, `billing_card`, `pace_alert` and `usage_log` exist, and a block not listed is on. An older `include_copilot_cli: false` reads as `copilot_cli` off |
| `model_aliases` | `{}` | models to merge, `{"<name>": "<name it merges into>"}`; names are matched exactly after normalising case, spacing, separators and a release date, and a target cannot itself be merged away |
| `model_colors` | `{}` | per-model chart colour, `{"<name>": {"family": "openai", "opacity": 0.6}}`; `family` is `google`, `anthropic`, `xai`, `openai`, `microsoft` or `other`, `opacity` runs 0.2 to 1 (left out, it follows the model's usage rank), and either may be left out |
| `telemetry_start_date` | `""` | first local day (`YYYY-MM-DD`) that counts; earlier records are left out of every total, empty counts them all |
| `session_names` | `{}` | custom session names, `{"<session id>": "<name>"}`, set from the session sheet; at most 120 characters |

A settings update with an unknown key, a malformed value or a bad colour is refused with its reason (`400`) and changes nothing; a bad value written by hand in `config.json` is ignored with a log warning.

Everything except the port is editable live from the ⚙ Settings dialog; each setting saves as you change it (numbers when you leave the field), with a toast, and there is no Save button.

### Optional: official billing card

Create a **fine-grained PAT** at <https://github.com/settings/personal-access-tokens> with the **"Plan" read-only** account permission, then:

```
cp .env.example .env     # and paste the token into GITHUB_COPILOT_BILLING_PAT
```

No PAT → the card simply explains it's not configured. The token is only ever sent to `api.github.com`. The card is always in one of five declared states (not configured, unavailable, ready, partial, unknown) and shows the reason when it cannot give figures; a refused token or an unreachable API is never shown as "no spend" ([docs/billing-state.md](docs/billing-state.md)).

Note: this is the **user** billing endpoint, so it works for individually-paid plans (Pro / Pro+). On a **Business / Enterprise seat the organization pays**, and this endpoint typically has no data for you — there, the local session stats plus the quota bar in VS Code's Copilot menu are your sources of truth.

### Optional: Telegram pace alert

Set `COPILOT_USAGE_TELEGRAM_BOT_TOKEN` (from [@BotFather](https://t.me/BotFather)) and `COPILOT_USAGE_TELEGRAM_CHAT_ID` in `.env` and restart. While the server runs it checks the cycle budget every 30 minutes and sends **one** message per billing cycle the first time the linear projection reaches 100% of `monthly_credits`. It waits until day 3 of the cycle unless the allowance is already used up, because one busy first morning projects absurdly. Which cycle has already alerted is kept in `pace-alert.json` in the app's data folder, the same folder as the usage log, and is written only after Telegram accepted the message, so a failed send is retried at the next check. Settings shows whether the alert is on and has a **Send test message** button. The token is only ever sent to `api.telegram.org`. The projection uses this machine's local data only.

## Optional: system tray (Windows)

`tray.bat` runs the server under a tray icon (Open dashboard / Open on private network / Restart / Quit; the private-network item appears only while Tailscale access is live) instead of a console window — handy for keeping it always on. It works directly on a fresh clone (it creates the venv and installs everything itself, including two small tray extras: pystray, pillow), so it is a full alternative to `start.bat`. Re-running it is a no-op while the tray lives; `tray.bat --restart` restarts it. Detection is scoped to this repo's `.venv`, so no other Python process is ever touched. Put a shortcut in `shell:startup` to launch on login.

## API

The dashboard is a thin client over a JSON API you can script against (interactive docs at `/api/docs`):

```
GET /health
GET /api/version        build identity (version, git_sha, built_at) behind the page footer
GET /api/summary?period=today|week|month|cycle|all|range:2026-11-03..2026-11-09   a range focuses every period-taking endpoint on those days
GET /api/trends?period=...&offset=0   the trend buckets one window back per offset, with the window's dates and whether older data exists
GET /api/sessions?period=...
GET /api/sessions/{session_id}
PUT /api/sessions/{session_id}/name   {"name": "..."} sets a custom name; an empty or null name restores the parsed title
GET /api/requests?period=...&project=&model=&mode=&ide=&result=ok|error&min_credits=&max_credits=&q=&sort=ts|credits|tokens|elapsed&order=asc|desc&page=&page_size=   the request explorer: rows, totals, facet counts
GET /api/insights?period=...   local rule-based insights: type, severity, one sentence, evidence
GET /api/context?period=...&label=   who drives the prompt tokens: projects, models, sessions, requests, plus coverage and the top `context_items`
GET /api/export.csv?period=...
GET /api/billing
GET /api/models          every model in the local history with its spellings, provider family and alias, for the Settings choices
GET /api/config          POST /api/config
GET /api/features        the feature blocks: switch, whether this machine can run each, and why not
PUT /api/features/{block_id}   {"enabled": true|false} switches one block, effective at once
GET /api/tailnet         the Tailscale option: saved switch, whether this server serves it, its URL, or why not
PUT /api/tailnet         {"enabled": true|false} saves the switch; the bind changes at the next server start
POST /api/refresh?force=false|true   rebuild the data now (force drops every parsed record and the read model first); joins a rebuild already running; `read_model` says whether records were kept on disk
POST /api/cache/clear   drop the snapshot, the cached summaries and every parsed record; the next read rebuilds
GET /api/health-check    whether a read-model health check is running, and its last result
POST /api/health-check   start one (202; 409 while one runs): it times the API over a temporary copy of the read model and never touches the live one
POST /api/alerts/test    send a Telegram test message (pace alert)
POST /api/usage/events   the page's batched view/tap events (local usage log)
GET /api/usage/summary   views and taps per control
GET /api/usage/time      foreground minutes: ?days=7&by=day|device|view
```

## Development

```
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # test + lint deps
.venv/Scripts/python.exe -m pytest      # tests (synthetic session fixtures)
.venv/Scripts/python.exe -m playwright install chromium   # once: the browser tests/e2e drive (phone and desktop viewports); skipped with a reason until it is installed
.venv/Scripts/python.exe -m app         # run without opening a browser
```

Verification gate (also needs Node, for `jscpd`): `powershell -File scripts/verify-before-ship.ps1`. Its last stdout line is `GATE-RESULT: PASS` or `GATE-RESULT: FAIL`; don't pipe it (a pipe hides the exit code), and treat a run without that line as not passed.

Layout: `app/` (FastAPI server, optional tray, the vanilla-JS dashboard in `app/webapp/static/`) · `src/` (discovery, the jsonl replay parser, aggregation, optional billing client) · `tests/` — see [docs/layout.md](docs/layout.md).

[UX surface map](docs/ux-surface-map.md): every tab, card, sheet, dialog, menu, state and gesture in one checklist with a diagram per area, generated from `docs/ux-surfaces.yaml`.

## Open from your phone (Tailscale)

Off by default: the server answers on `127.0.0.1` only. Turn on **Settings → Available features → Tailscale access** (or set `"tailnet_access": true`) and restart (`tray.bat --restart`); the server then also serves HTTPS on this machine's Tailscale address, on the same port, with the certificate from `tailscale cert`, and Settings shows the URL to open (the tray menu has an Open on private network item for the same address). It needs the `tailscale` command, a connected tailnet with MagicDNS and HTTPS Certificates enabled. If any of that is missing the server stays on loopback and Settings and the log say which. It never binds `0.0.0.0`, and anyone who can reach that address on your tailnet can use the dashboard and its settings. Details: [docs/setup-and-migration.md](docs/setup-and-migration.md).

## Privacy

Read-only over Copilot's own local log files; no subprocesses except the `tailscale` command, and only while the Tailscale option above is on; binds to `127.0.0.1` only, plus your own Tailscale address when you turn that option on; no telemetry; the only outbound call is the optional GitHub billing API when *you* configure a PAT (the Tailscale option fetches its certificate through your local Tailscale client). Your prompts stay on your machine — the dashboard shows the first ~200 characters of each VS Code request message (VS Code itself truncates it in its log) and the whole prompt of a Copilot CLI request, cut visually in lists, and the CSV export contains the same text.

**Usage log.** Besides reading Copilot's files, the dashboard records its own use — which view or control was used, a random per-visit id, the device class and timestamps, never prompts, names, URLs or addresses — into `usage.sqlite3`, one SQLite file on the machine serving it, in `C:\sqlite\github-copilot-usage\` on Windows and `~/.local/share/sqlite/github-copilot-usage/` elsewhere (created on first write). Set `GITHUB_COPILOT_USAGE_DATA_DIR` to use another directory, or `FLEET_DATA_ROOT` to use `<root>/github-copilot-usage/`; either may live in `.env`. Rows older than a year, or beyond 200,000, are pruned; nothing leaves the machine, and deleting the file resets it.

## License

MIT — see [LICENSE](LICENSE). Share it with your team.
