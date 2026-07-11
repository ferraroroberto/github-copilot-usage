# Project Instructions

Canonical instructions for AI coding agents working in this repository. Claude Code reads this file directly as project memory. Other agents (Cursor, Codex, etc.) reach it via the one-line `AGENTS.md` pointer.

## This repository

Standalone, public, local-only dashboard for GitHub Copilot usage: a FastAPI JSON API + vanilla-JS SPA on `127.0.0.1:8377`, parsing the session logs Copilot itself writes (VS Code chat sessions, optional Copilot CLI, optional GitHub billing API). See `README.md` for setup, layout, data sources, and honest limitations.

**Standalone by design.** This repo is shared publicly and with colleagues on locked-down corporate machines. Hard rules that follow from that:

- No references to any private infrastructure, other local repos, or machine-specific paths.
- No CDNs or external assets — vendor what the frontend needs (`static/vendor/`).
- Anything that can be absent on a given machine (Copilot CLI, billing PAT, tray extras, a particular editor variant) must degrade gracefully with a visible reason, never crash the page or the server.

## Constraints worth knowing

- **Python 3.9+ compatibility** (corporate machines run old interpreters): `from __future__ import annotations` everywhere, `Optional[T]`/`List[T]` typing, no `match` statements.
- **Runtime deps stay at three** (fastapi, uvicorn, httpx). Tray extras (pystray, pillow) live only in `requirements-tray.txt`; dev tooling in `requirements-dev.txt`. Adding a runtime dependency needs a strong reason — every extra wheel is proxy pain for someone.
- **Parsers are read-only, zero subprocesses, mtime-cached** — never write to or lock Copilot's own files; re-parse only when mtime/size changes (the SPA polls every 30 s).
- **Period bucketing uses local dates deliberately** (a human-facing report, "today" = today at your desk); the optional billing API card is the UTC-exact counterpart. Don't "fix" one to match the other.
- **Credits are the primary unit** (1 credit = 1 premium-request unit = $0.01 under GitHub's AI-credits model); USD is always derived, never stored.
- Server binds to `127.0.0.1` only. Keep it that way.

## Run / restart

- **Dev (foreground):** `.venv\Scripts\python.exe -m app` (`--open` also launches the browser; `--port N` overrides). Ctrl-C to stop.
- **Resident (Windows tray):** `tray.bat` — self-bootstrapping (creates `.venv`, installs core + tray deps on first run) and idempotent (no-op if already running). **`tray.bat --restart` is the canonical safe restart:** detection and kill are scoped to this repo's `.venv` path in the process command line — never blanket-kill python/pythonw, other processes are untouched.
- **Build confirmation:** `GET http://127.0.0.1:8377/health` returns 200 and carries `version` (single source: `app/version.py`).

## Verification gate

```
.venv\Scripts\python.exe -m compileall -q app tests
.venv\Scripts\python.exe -m pytest -q
```

For changes with a runtime surface, additionally hit `/api/summary?period=all` on a running instance and load `/` in a browser (both themes) before calling it done. There is no CI — the gate is local.

## UX surface

- design spec applies: yes
- paths:
  - static/**/*.css
  - static/**/*.{js,html}
- key views:
  - /    (single-page dashboard; session-detail and settings dialogs)

Chart palettes are CVD-validated (fixed assignment order, model keeps its color, 8th+ series folds into gray "Other"); if you touch chart colors, re-validate for both themes before shipping.
