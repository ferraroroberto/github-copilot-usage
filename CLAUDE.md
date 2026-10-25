# Project Instructions

## This repository

Standalone, public, local-only dashboard for GitHub Copilot usage: a FastAPI JSON API + vanilla-JS SPA on `127.0.0.1:8377`, parsing the session logs Copilot itself writes (VS Code chat sessions, optional Copilot CLI, optional GitHub billing API). `README.md` carries setup, layout, data sources and honest limitations.

**Standalone by design** — this repo is shared publicly and with colleagues on locked-down corporate machines:

- No references to any private infrastructure, other local repos, or machine-specific paths.
- No CDNs or external assets — vendor what the frontend needs (`app/webapp/static/vendor/`).
- Anything that can be absent on a given machine (Copilot CLI, billing PAT, tray extras, a particular editor variant) must degrade gracefully with a visible reason, never crash the page or the server.

## Constraints worth knowing

- **Python 3.9+ compatibility** (corporate machines run old interpreters): `from __future__ import annotations` everywhere, `Optional[T]`/`List[T]` typing, no `match` statements.
- **Runtime deps stay at three** (fastapi, uvicorn, httpx). Tray extras (pystray, pillow) live only in `requirements-tray.txt`; dev tooling in `requirements-dev.txt`. Adding a runtime dependency needs a strong reason (every extra wheel is proxy pain).
- **Parsers are read-only, zero subprocesses, mtime-cached** — never write to or lock Copilot's own files; re-parse only when mtime/size changes (the SPA polls every 30 s).
- **Period bucketing uses local dates deliberately** ("today" = today at your desk); the optional billing API card is the UTC-exact counterpart. Don't "fix" one to match the other.
- **Credits are the primary unit** (1 credit = 1 premium-request unit = $0.01 under GitHub's AI-credits model); USD is always derived, never stored.
- Server binds to `127.0.0.1` only. Keep it that way.

## Run / restart

- **Dev (foreground):** `.venv\Scripts\python.exe -m app` (`--open` also launches the browser; `--port N` overrides). Ctrl-C to stop.
- **Resident (Windows tray):** `tray.bat` — self-bootstrapping (creates `.venv`, installs core + tray deps on first run) and idempotent (no-op if already running). **`tray.bat --restart` is the canonical safe restart:** detection and kill are scoped to this repo's `.venv` path in the process command line — never blanket-kill python/pythonw, other processes are untouched.
- **Build confirmation:** `GET http://127.0.0.1:8377/health` returns 200 and carries `version` (single source: `app/version.py`).

## Verification gate

```
powershell -File scripts/verify-before-ship.ps1
```

That runs `compileall -q app src tests`, `pytest -q`, the structure ratchet, the instruction ratchet (`scripts/instruction_ratchet.py`, vendored byte-for-byte like the structure ratchet; it holds `CLAUDE.md`/`AGENTS.md` to the prompt-audit lint rules and size caps against `.instruction-ratchet-baseline.json`, currently empty) and the doc-refs ratchet (`scripts/doc_refs_ratchet.py`, also vendored; it fails on a README/CLAUDE.md/`docs/` reference to a path, module, link or port that does not exist, against `.doc-refs-ratchet-baseline.json`, currently empty), the dead-code ratchet (`scripts/dead_code_ratchet.py`, also vendored; it fails on a new Python function, class or method nothing refers to, against `.dead-code-ratchet-baseline.json`, currently empty) and the static ratchet (`scripts/static_ratchet.py`, also vendored; it fails on a new duplicated block, unused import or variable, or likely-bug pattern, against `.static-ratchet-baseline.json`, currently empty; it needs Node for `jscpd` and `ruff` from `requirements-dev.txt`, and a detector that cannot run exits 3, which fails the gate). There is no CI — the gate is local. For changes with a runtime surface, additionally hit `/api/summary?period=all` on a running instance and load `/` in a browser (both themes) before calling it done.

## Structure ratchet

The gate fails when a file passes 1,000 lines, a function 100 lines, a directory 40 modules, when `src/` imports `app/`, or when a root folder falls outside the `webapp-tray` shape in `.fleet.toml`; baselined offenders (`.structure-ratchet-baseline.json`, currently empty) may not grow. Exit 3 (not confirmed) is a failure, never a pass. `scripts/structure_ratchet.py` is vendored byte-for-byte (`.fleet.toml` `[vendored]`); never edit it here. Lower the baseline with `--update-baseline`; `--update-baseline --allow-additions` is the only way to add an entry and shows in review. Where code goes: `docs/layout.md`. The script needs Python 3.11+; the app itself still runs on 3.9+.

## UX surface

- design spec applies: yes
- paths:
  - app/webapp/static/**/*.css
  - app/webapp/static/**/*.{js,html}
- key views:
  - /    (single-page dashboard; session-detail and settings dialogs)

Chart palettes are CVD-validated (fixed assignment order, model keeps its color, 8th+ series folds into gray "Other"); if you touch chart colors, re-validate for both themes before shipping.

The light palette matches `design.md` exactly; the dark palette matches `design.dark.md` (which is itself GitHub's dark palette this app deliberately mirrors — see `app/webapp/static/styles.css:1-2`).

### Design-system conformance exceptions

`/design-sync` (fleet-config `design_lint.py`) emits one finding against this app that is an **accepted exception**, not drift to fix:

- **hit-target WARN (`.model-dot`, 10×10px)** — accepted. `.model-dot` is a decorative colour swatch (`<span>` in the models table, `app/webapp/static/app.js`), not a pointer target, so the 44px floor (design.md Touch targets, which governs *pointer* targets) doesn't apply; the lint flags it only because "del" in "model" trips its interactive-selector heuristic. The two genuinely-interactive compact controls (`.icon-btn`, `.dialog-close`) carry the invisible `::before` hit-area expansion.
- **nav-contract — fixed, no longer a WARN (`ferraroroberto/github-copilot-usage#22`).** The bottom-tab pill still doesn't apply — single-view dashboard, the only `<nav>` is the period segmented control (`app/webapp/static/index.html`, `class="seg"`), not primary navigation, no `_vendored/nav/` to adopt. The shell mechanics the nav contract requires are carried for the installed PWA: `app/webapp/static/styles.css` has a `@media (display-mode: standalone)` block making `.app` the fixed-inset scroller (viewport-anchored `100dvh`/`100lvh` sizing, `env(safe-area-inset-bottom)`, `body:has(dialog[open])` hide-under-dialog). If this app ever grows a second top-level view, still adopt `_vendored/nav/` verbatim on top of this shell — never re-author it.
- **Re-audit log** — append a line whenever a re-audit lands (don't rewrite existing entries), so the next auditor sees they were checked, not missed. `static/` was byte-unchanged through 2026-09-03; #22 (2026-09-10) is the first change.
  - 2026-07-31 — #8: both WARNs filed.
  - 2026-08-07 — #10: both reconfirmed.
  - 2026-08-16 — #12: both reconfirmed.
  - 2026-08-20 — #14: lint self-triaged `.model-dot` out of *filed* findings (still emitted); nav-contract reconfirmed accepted.
  - 2026-08-27 — #17: nav-contract reconfirmed accepted — still single-view, no bottom tab bar.
  - 2026-09-03 — #20: `static/` byte-unchanged since #17 (`git log e91d9aa..HEAD -- static/` empty): re-emission, not new drift; nav-contract reconfirmed accepted.
  - 2026-09-10 — #22: nav-contract WARN resolved by the standalone fixed-inset `.app` shell (above) — `design_lint contracts` PASS. `hit-target` WARN is the sole accepted exception.
  - 2026-10-07 — #25: `--font-label`/`--font-caption` aligned to the spec scale (0.875rem/0.75rem); global `font: inherit` form-control reset added; text-size setting adopted (pre-paint `data-textsize` stamp in `index.html` + Small/Default/Large control in Settings, written in-repo not vendored, to keep the repo free of references to other local repos); token-direction `↑`/`↓` replaced by inline Lucide arrows. Two prose arrows in `index.html` stay as copy (accepted `glyph-icons` WARN, low materiality); `hit-target` and the unmeasured rendered leg unchanged.
  - 2026-10-24 — #28: `static/` moved byte-for-byte to `app/webapp/static/` (`git mv`) for the structure-ratchet layout; no design change, `hit-target` unchanged.

**App icons — self-contained, no `brand_gen`.** The installable-PWA icon family (`icon-180/192/512`, distinct `icon-512-maskable`, multi-size `favicon.ico`) + `app/webapp/static/manifest.webmanifest` are generated by `scripts/gen_icons.py`, a standalone Pillow-only script that reproduces this app's own `app/webapp/static/favicon.svg` brand geometry (GitHub-blue tile, white column-chart bars). It does **not** import project-scaffolding's shared `brand_gen` generator: this repo ships publicly and may not reference other local repos / private infra. The generated assets are committed; regenerate after a brand change with `.venv\Scripts\python.exe scripts\gen_icons.py`. (`design_lint`'s app-icon-family check keys on the `brand_gen` contract name + a `render_set()` call in `scripts/`, both satisfied by this local implementation of the same contract.)
