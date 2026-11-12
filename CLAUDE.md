# Project Instructions

## This repository

Standalone, public, local-only dashboard for GitHub Copilot usage: a FastAPI JSON API + vanilla-JS SPA on `127.0.0.1:8377`, parsing the session logs Copilot itself writes (VS Code chat sessions, optional Copilot CLI, optional GitHub billing API). `README.md` carries setup, layout, data sources and honest limitations.

**Standalone by design** — this repo is shared publicly and with colleagues on locked-down corporate machines:

- No references to any private infrastructure, other local repos, or machine-specific paths.
- No CDNs or external assets — vendor what the frontend needs (`app/webapp/static/vendor/`).
- Anything that can be absent on a given machine (Copilot CLI, billing PAT, tray extras, a particular editor variant) must degrade gracefully with a visible reason, never crash the page or the server.

## Constraints worth knowing

- **Python 3.9+ compatibility** (old corporate interpreters): `from __future__ import annotations` everywhere, `Optional[T]`/`List[T]` typing, no `match` statements.
- **Runtime deps stay at three** (fastapi, uvicorn, httpx). Tray extras (pystray, pillow) live only in `requirements-tray.txt`; dev tooling in `requirements-dev.txt`. A new runtime dependency needs a strong reason (every wheel is proxy pain).
- **Parsers are read-only, zero subprocesses, mtime-cached** — never write to or lock Copilot's own files; re-parse only when mtime/size changes (the SPA polls every 30 s).
- **Period bucketing uses local dates deliberately** ("today" = today at your desk); the optional billing API card is the UTC-exact counterpart. Don't "fix" one to match the other.
- **Credits are the primary unit** (1 credit = 1 premium-request unit = $0.01 under GitHub's AI-credits model); USD is always derived, never stored.
- Server binds to `127.0.0.1` only.
- **Static assets are served through `CachingStaticFiles`** (`src/static_versioning.py`), never a bare `StaticFiles`: one content hash is stamped as `?v=` onto the shell's asset URLs and the ES-module imports, `.js`/`.css` are immutable, and `index.html` always revalidates, so an installed phone PWA picks up a new build on a normal reload. No service worker.

## Run / restart

- **Dev (foreground):** `.venv\Scripts\python.exe -m app` (`--open` also launches the browser; `--port N` overrides).
- **Resident (Windows tray):** `tray.bat` — self-bootstrapping (creates `.venv`, installs core + tray deps) and idempotent. **`tray.bat --restart` is the canonical safe restart:** detection and kill are scoped to this repo's `.venv` path in the process command line — never blanket-kill python/pythonw.
- **Build confirmation:** `GET http://127.0.0.1:8377/health` returns 200 and carries `version` (single source: `app/version.py`).

## Verification gate

```
powershell -File scripts/verify-before-ship.ps1
```

That runs `compileall -q app src tests`, `pytest -q`, and these ratchets, each a `scripts/<name>_ratchet.py` vendored byte-for-byte (`.fleet.toml` `[vendored]`) with a currently empty baseline `.<name>-ratchet-baseline.json`:

- **structure** (below).
- **instruction**: holds `CLAUDE.md`/`AGENTS.md` to the prompt-audit lint rules and size caps.
- **doc-refs**: fails on a README/CLAUDE.md/`docs/` reference to a path, module, link or port that does not exist.
- **dead-code**: fails on a new Python function, class or method nothing refers to.
- **static**: fails on a new duplicated block, unused import or variable, or likely-bug pattern; needs Node for `jscpd` and `ruff` from `requirements-dev.txt`; a detector that cannot run exits 3, which fails the gate.
- **docs**: fails when the root `README.md` outgrows its size budget or `README.md`/`docs/` gain an issue reference, dated heading or changelog heading.
- **comment**: fails when a product-code comment gains an issue reference.
- **conventions**: rules `no-window`, `logging-not-print`, `no-drive-path` and `subprocess-timeout` fail when a source file gains a spawn without `creationflags`, a `print` in library code, a hardcoded drive path or a subprocess call without `timeout=` (or a `# subprocess-timeout-exempt: <reason>` marker).

There is no CI — the gate is local. For changes with a runtime surface, additionally hit `/api/summary?period=all` on a running instance and load `/` in a browser (both themes) before calling it done.

## Structure ratchet

The structure ratchet's limits, baseline and Python requirement are in `docs/layout.md`. `scripts/structure_ratchet.py` is vendored byte-for-byte (`.fleet.toml` `[vendored]`); never edit it here. Lower the baseline with `--update-baseline`; `--update-baseline --allow-additions` is the only way to add an entry and shows in review.

## UX surface

- design spec applies: yes
- paths:
  - app/webapp/static/**/*.css
  - app/webapp/static/**/*.{js,html}
- key views:
  - /    (single-page dashboard; session-detail and settings dialogs)

Chart palettes are CVD-validated (fixed assignment order, model keeps its color, 8th+ series folds into gray "Other"); if you touch chart colors, re-validate for both themes before shipping.

The light palette matches `design.md` exactly; the dark palette matches `design.dark.md` (GitHub's dark palette, deliberately mirrored — see `app/webapp/static/styles.css:1-2`).

### Design-system conformance exceptions

`/design-sync` (fleet-config `design_lint.py`) emits two findings that are **accepted exceptions**, not drift to fix; everything else, including the nav contract, is `design_lint contracts` PASS:

- **hit-target WARN (`.model-dot`, 10×10px)** — `.model-dot` is a decorative colour swatch (`<span>` in the models table, `app/webapp/static/app.js`), not a pointer target, so the 44px floor (design.md Touch targets) doesn't apply; the lint flags it only because "del" in "model" trips its interactive-selector heuristic. The genuinely interactive compact controls (`.icon-btn`, `.dialog-close`) carry the invisible `::before` hit-area expansion.
- **glyph-icons WARN (two prose arrows in `index.html`)** — low materiality; the arrows stay as copy.

The bottom-tab pill nav contract doesn't apply: single-view dashboard, the only `<nav>` is the period segmented control (`app/webapp/static/index.html`, `class="seg"`), not primary navigation, and there is no `_vendored/nav/`. The installed PWA uses the window as its scroller (no fixed-inset `.app` shell); `app/webapp/static/styles.css` hides page scroll under an open `<dialog>` and pads `.app` by `env(safe-area-inset-bottom)`, and `index.html` sets `apple-mobile-web-app-status-bar-style` to `default`. If the app ever grows a second top-level view, adopt `_vendored/nav/` verbatim — never re-author it.

**App icons — self-contained, no `brand_gen`.** The PWA icon family (`icon-180/192/512`, distinct `icon-512-maskable`, multi-size `favicon.ico`) + `app/webapp/static/manifest.webmanifest` come from `scripts/gen_icons.py`, a standalone Pillow-only script reproducing `app/webapp/static/favicon.svg` (GitHub-blue tile, white column-chart bars). It must not import project-scaffolding's `brand_gen`: this repo is public and may not reference other local repos. The generated assets are committed; regenerate after a brand change with `.venv\Scripts\python.exe scripts\gen_icons.py`. (`design_lint`'s app-icon-family check keys on the `brand_gen` contract name + a `render_set()` call in `scripts/`, both satisfied here.)
