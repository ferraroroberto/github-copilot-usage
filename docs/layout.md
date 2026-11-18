# Layout

```
github-copilot-usage/
├── app/                       # the UI process: server, tray, entry point
│   ├── __main__.py            # `python -m app` — argparse, uvicorn, optional browser open
│   ├── server.py              # FastAPI routes (JSON API + serves the dashboard); no parsing logic
│   ├── listeners.py           # the sockets served: loopback over HTTP always, the Tailscale address over HTTPS when opted in, on one event loop
│   ├── tray.py                # optional Windows tray icon (pystray) that owns the listeners
│   ├── build_info.py          # short commit + process start time for the footer
│   ├── version.py             # single source of the version reported by /health
│   └── webapp/
│       └── static/            # the vanilla-JS SPA: index.html (the four tab panes under one page header), app.js, rows.js (the one list-row renderer: sessions, insights, requests and the drill-down all draw the vendored action-row through it), requests.js (the request explorer view), insights.js (the insights card, session diagnostics and the context drill-down), features.js (Settings → Available features: the switches and what hides while a block is off), tailnet.js (the Tailscale access row in that group), models.js (model colours by provider family and the Settings sheet that merges models and overrides a colour), charts.js (the two trend charts), timeline.js (trend windows, the focused date range and the coverage note), tokens.js (token figures as printed: a count the source did not record is a dash, never 0), sections.js (collapsible cards), session-name.js (the rename row of the session sheet), styles.css, PWA manifest + icons, vendored Chart.js
│
├── src/                       # logic layer (never imports `app`)
│   ├── config.py              # config.json + env loading, thread-safe settings
│   ├── features.py            # the feature blocks switched per machine: their ids, what each needs here, the switch
│   ├── tailnet.py             # the opt-in Tailscale bind: reads the machine's tailnet address and name through the tailscale command, fetches the certificate, says why it cannot
│   ├── discovery.py           # finds Copilot's session-log folders per editor variant and OS
│   ├── vscode_parser.py       # read-only, mtime-cached parser for VS Code chat session logs
│   ├── cli_parser.py          # read-only, mtime-cached parser for Copilot CLI session logs
│   ├── cli_session_store.py   # read-only reader for the Copilot CLI's session-store.db per-call usage
│   ├── refresh.py             # the shared snapshot of parsed records: one joined rebuild, survives a cancelled request, never overwritten by an older build, plus the summary cached on a content fingerprint
│   ├── aggregate.py           # period bucketing, per-model and per-session rollups, CSV export
│   ├── model_labels.py        # conservative model-label grouping, exact aliases, provider families and the validation of the model settings
│   ├── request_explorer.py    # the request explorer query: filters, sorting, paging and facet counts over the parsed requests
│   ├── prompt_composition.py  # one reading of a request's logged composition: label, category and estimated tokens
│   ├── context_drilldown.py   # who carries the prompt tokens: top projects, models, sessions and requests, with coverage
│   ├── session_diagnostics.py # per-session heuristic flags: large prompt, growing prompt, high non-user share
│   ├── insights.py            # deterministic local insights with a stable type, severity and linkable evidence
│   ├── static_versioning.py   # content-hash cache busting: the ?v= stamp, CachingStaticFiles and the stamped index.html
│   ├── shell_etag.py          # vendored byte-for-byte; the shell's weak ETag and the 200-or-304 answer for a conditional GET
│   ├── billing.py             # optional GitHub billing API client (needs a token; absent → visible reason)
│   ├── billing_state.py       # the billing card's declared state and its one derive function (docs/billing-state.md)
│   ├── pace_alert.py          # optional Telegram message once per cycle when the projection reaches the allowance
│   ├── usage_log.py           # vendored byte-for-byte; the local view/tap log: `/api/usage/*` router, SQLite store and foreground-time rollup
│   ├── runtime_data.py        # vendored byte-for-byte; the runtime-data root and its `FLEET_DATA_ROOT` / `<APP>_DATA_DIR` overrides
│   └── usage_store.py         # the usage database path, resolved through `runtime_data`; creates nothing
│
├── tests/                     # pytest suite, fixtures only — no real logs or credentials; test_python_floor.py holds the 3.9 promise; e2e/ drives the dashboard in a phone-sized browser when Playwright is installed, through one shared Chromium context and a keep-alive test server so a run leaves few TIME_WAIT sockets, and names a drained port pool as its own failure (conftest.py)
├── scripts/
│   ├── verify-before-ship.ps1 # the local gate: byte-compile, tests, structure, instruction, doc-refs, dead-code, static, docs, comment and conventions ratchets
│   ├── structure_ratchet.py   # vendored byte-for-byte; see "Structure ratchet" below
│   ├── instruction_ratchet.py # vendored byte-for-byte; holds the instruction files to the prompt-audit rules
│   ├── doc_refs_ratchet.py    # vendored byte-for-byte; fails on dangling doc references
│   ├── dead_code_ratchet.py   # vendored byte-for-byte; fails on new unreferenced definitions
│   ├── static_ratchet.py      # vendored byte-for-byte; fails on new duplicates, unused names and bug patterns
│   ├── docs_ratchet.py        # vendored byte-for-byte; fails on a growing README or new history markers in the docs
│   ├── comment_ratchet.py     # vendored byte-for-byte; fails on new issue references in code comments
│   ├── conventions_ratchet.py # vendored byte-for-byte; fails on new spawns without creationflags, library prints, drive paths and a bare StaticFiles
│   ├── conventions_readers.py # vendored byte-for-byte; the syntax-tree and line readers conventions_ratchet loads
│   ├── gen_icons.py           # regenerates the PWA icon family under app/webapp/static/
│   └── tray_lifecycle.ps1     # detect / stop / start / verify the tray, called by `tray.bat`
└── docs/                      # durable reference (this file; setup-and-migration.md for a new machine and what to copy)
```

Root files: `start.bat` (foreground launch on Windows; `scripts/start.sh` is the macOS / Linux equivalent), `tray.bat` (resident Windows tray, self-bootstrapping), `requirements*.txt`, `config.example.json`, `.env.example`, `.fleet.toml` (shape and vendored manifest), `.structure-ratchet-baseline.json`.

## Where new code goes

- A new route or response shape → `app/server.py`; keep it a thin wrapper over a `src/` function.
- Anything that reads logs, buckets periods, computes credits or talks to an API → `src/`. `src/` is import-free of `app/`, which the ratchet enforces, so the logic stays testable without FastAPI.
- A new front-end asset → `app/webapp/static/`; vendor third-party libraries under `static/vendor/`, never a CDN.
- A new test → `tests/`, built on fixtures.

## Structure ratchet

`scripts/structure_ratchet.py` fails the gate when a file passes 1,000 lines, a function 100 lines, a directory 40 modules, when `src/` imports `app/`, or when a root folder falls outside the declared shape. It needs Python 3.11+ (`tomllib`); the app itself still runs on 3.9+. Existing offenders are recorded in `.structure-ratchet-baseline.json` at their size and may not grow; the baseline is currently empty. Exit 3 means "not confirmed" and fails the gate like exit 1.

## Declared exceptions

`shape = "webapp-tray"` in `.fleet.toml`, with an empty `extra_roots`: every root folder is one the preset lists, so no exception is declared.
