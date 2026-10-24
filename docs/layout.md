# Layout

```
github-copilot-usage/
├── app/                       # the UI process: server, tray, entry point
│   ├── __main__.py            # `python -m app` — argparse, uvicorn, optional browser open
│   ├── server.py              # FastAPI routes (JSON API + serves the dashboard); no parsing logic
│   ├── tray.py                # optional Windows tray icon (pystray) that owns the server thread
│   ├── version.py             # single source of the version reported by /health
│   └── webapp/
│       └── static/            # the vanilla-JS SPA: index.html, app.js, styles.css, PWA manifest + icons, vendored Chart.js
│
├── src/                       # logic layer (never imports `app`)
│   ├── config.py              # config.json + env loading, thread-safe settings
│   ├── discovery.py           # finds Copilot's session-log folders per editor variant and OS
│   ├── vscode_parser.py       # read-only, mtime-cached parser for VS Code chat session logs
│   ├── cli_parser.py          # read-only, mtime-cached parser for Copilot CLI session logs
│   ├── aggregate.py           # period bucketing, per-model and per-session rollups, CSV export
│   └── billing.py             # optional GitHub billing API client (needs a token; absent → visible reason)
│
├── tests/                     # pytest suite, fixtures only — no real logs or credentials
├── scripts/
│   ├── verify-before-ship.ps1 # the local gate: byte-compile, tests, structure, instruction, doc-refs and dead-code ratchets
│   ├── structure_ratchet.py   # vendored byte-for-byte; see "Structure ratchet" below
│   ├── instruction_ratchet.py # vendored byte-for-byte; holds the instruction files to the prompt-audit rules
│   ├── doc_refs_ratchet.py    # vendored byte-for-byte; fails on dangling doc references
│   ├── dead_code_ratchet.py   # vendored byte-for-byte; fails on new unreferenced definitions
│   ├── gen_icons.py           # regenerates the PWA icon family under app/webapp/static/
│   └── tray_lifecycle.ps1     # detect / stop / start / verify the tray, called by `tray.bat`
└── docs/                      # durable reference (this file)
```

Root files: `start.bat` / `start.sh` (foreground launch), `tray.bat` (resident Windows tray, self-bootstrapping), `requirements*.txt`, `config.example.json`, `.env.example`, `.fleet.toml` (shape and vendored manifest), `.structure-ratchet-baseline.json`.

## Where new code goes

- A new route or response shape → `app/server.py`; keep it a thin wrapper over a `src/` function.
- Anything that reads logs, buckets periods, computes credits or talks to an API → `src/`. `src/` is import-free of `app/`, which the ratchet enforces, so the logic stays testable without FastAPI.
- A new front-end asset → `app/webapp/static/`; vendor third-party libraries under `static/vendor/`, never a CDN.
- A new test → `tests/`, built on fixtures.

## Structure ratchet

`scripts/structure_ratchet.py` fails the gate when a file passes 1,000 lines, a function 100 lines, a directory 40 modules, when `src/` imports `app/`, or when a root folder falls outside the declared shape. It needs Python 3.11+ (`tomllib`); the app itself still runs on 3.9+. Existing offenders are recorded in `.structure-ratchet-baseline.json` at their size and may not grow; the baseline is currently empty. Exit 3 means "not confirmed" and fails the gate like exit 1.

## Declared exceptions

`shape = "webapp-tray"` in `.fleet.toml`, with an empty `extra_roots`: every root folder is one the preset lists, so no exception is declared.
