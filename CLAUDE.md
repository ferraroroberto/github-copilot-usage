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
- **Resident (Windows tray):** `tray.bat` — self-bootstrapping (creates `.venv`, installs core + tray deps) and idempotent. **`.\.venv\Scripts\python.exe -m src.restart_check restart --port 8377` (`./.venv/Scripts/python.exe` in Git Bash; same in every shell; run it in the foreground) is the canonical safe restart:** it runs this checkout's `tray.bat --restart` by absolute path and exits non-zero unless a different process serves `HEAD`; detection and kill are scoped to this repo's `.venv` path in the process command line — never blanket-kill python/pythonw. Never type the bare `tray.bat --restart`: it is not found from any shell here (by hand only `.\tray.bat` in PowerShell, `./tray.bat` in Git Bash), and Git Bash `cmd.exe /c "tray.bat --restart"` exits 0 having restarted nothing.
- **Build confirmation:** `GET http://127.0.0.1:8377/health` returns 200 and carries `version` (single source: `app/version.py`) and `git_sha`, the checkout's short commit; a restart is confirmed by that sha changing, not only by the process start time.

## Verification gate

```
powershell -File scripts/verify-before-ship.ps1
```

That runs `compileall -q app src tests`, `pytest -q`, and these ratchets, each a `scripts/<name>_ratchet.py` vendored byte-for-byte (`.fleet.toml` `[vendored]`) with a baseline `.<name>-ratchet-baseline.json` (empty, except the request-budget one below). **The exact stage count and order are declared once, in `scripts/verify-before-ship.ps1`'s own header comment** — treat that as canonical; this list only adds what each ratchet checks beyond its name, and `docs/layout.md` points here instead of repeating it:

- **structure** (below).
- **instruction**: holds `CLAUDE.md`/`AGENTS.md` to the prompt-audit lint rules and size caps.
- **doc-refs**: fails on a README/CLAUDE.md/`docs/` reference to a path, module, link or port that does not exist.
- **dead-code**: fails on a new Python function, class or method nothing refers to.
- **static**: fails on a new duplicated block, unused import or variable, or likely-bug pattern; needs Node for `jscpd` and `ruff` from `requirements-dev.txt`; a detector that cannot run exits 3, which fails the gate.
- **docs**: fails when the root `README.md` outgrows its size budget or `README.md`/`docs/` gain an issue reference, dated heading or changelog heading.
- **comment**: fails when a product-code comment gains an issue reference.
- **conventions**: rules `no-window`, `logging-not-print`, `no-drive-path` and `subprocess-timeout` fail when a source file gains a spawn without `creationflags`, a `print` in library code, a hardcoded drive path or a subprocess call without `timeout=` (or a `# subprocess-timeout-exempt: <reason>` marker).
- **runtime-write**: a snapshot/probe pair bracketing `pytest`, plus its own stage, fails when a write target resolves under a code folder instead of the runtime-data root.
- **usage-track**: fails on a web control with no constant tracking id, a `<dialog>` with no `data-track-view`, a built id, or an unwired usage log.
- **ux-surface**: fails on a surface root (`<dialog>`, a `dialog`/`menu`/`tabpanel` role, a `popover`) with no constant `data-surface` id, an id with no entry in `docs/ux-surfaces.yaml`, an entry no element carries, one id on two surfaces, a link to an id with no entry, or a `docs/ux-surface-map.md` not regenerated from the map.
- **request-budget**: fails when a file's count for a request-budget rule rises above `.request-budget-ratchet-baseline.json`: a fixed-rate browser poll, a poll under 2 s, an undeclared fast or subprocess-spawning server loop, a use of the default thread pool, or a server without the request-timing middleware. Its baseline holds the waste that exists today; lower it with `--update-baseline`, never raise it, and declare a loop that must run while nobody is viewing in `.fleet.toml` `[[request_budget.loops]]` with its reason.

There is no CI — the gate is local. For changes with a runtime surface, additionally hit `/api/summary?period=all` on a running instance and load `/` in a browser (both themes) before calling it done.

## Structure ratchet

The structure ratchet's limits, baseline and Python requirement are in `docs/layout.md`. `scripts/structure_ratchet.py` is vendored byte-for-byte (`.fleet.toml` `[vendored]`); never edit it here. Lower the baseline with `--update-baseline`; `--update-baseline --allow-additions` is the only way to add an entry and shows in review.

## UX surface

- design spec applies: yes
- paths:
  - app/webapp/static/**/*.css
  - app/webapp/static/**/*.{js,html}
- key views:
  - /    (four tabs: Overview, Trends, Breakdown, Sessions; session-detail, requests and settings dialogs)

Chart palettes are CVD-validated (provider-family hues in a fixed order, one lightness step per model rank, a model keeps its color, a fourth model of a family folds into gray); if you touch chart colors (`app/webapp/static/models.js`), re-validate for both themes before shipping.

The light palette matches `design.md` exactly; the dark palette matches `design.dark.md` (GitHub's dark palette, deliberately mirrored — see `app/webapp/static/styles.css:1-2`).

### UX surface map

A change that adds, removes, renames or reshapes a surface a person can reach (a tab, card, sheet, dialog, menu, Settings row, empty or error state, gesture) updates its entry in `docs/ux-surfaces.yaml` in the same change, keeps the surface root's `data-surface` id equal to the entry's `id`, and regenerates the page with `.venv\Scripts\python.exe scripts\ux_surface_map.py`. Never edit `docs/ux-surface-map.md` by hand; the `ux-surface` ratchet fails a page the map does not generate. The vendored row menu builds its own element, so its id is stamped at the call site in `app.js`.

### Design-system conformance exceptions

`/design-sync` (fleet-config `design_lint.py`) emits findings that are **accepted exceptions**, not drift to fix; each is a `[[design.accepted]]` entry in `.fleet.toml` (the lint reports it as ACCEPTED), and everything else, including the nav contract, is `design_lint contracts` PASS:

- **hit-target WARN (`.model-dot`, 10×10px)** — `.model-dot` is a decorative colour swatch (`<span>` in the models table, `app/webapp/static/app.js`), not a pointer target, so the 44px floor (design.md Touch targets) doesn't apply; the lint flags it only because "del" in "model" trips its interactive-selector heuristic. The genuinely interactive compact controls (the vendored `.icon-button`, which includes the sheet's `.detail-close`) carry the invisible `::before` hit-area expansion.
- **glyph-icons WARN (the `Settings → …` prose arrow in `index.html`)** — low materiality; it is copy, not an icon, and matches the docs' wording.

Navigation is the vendored `_vendored/nav/` bottom-tab pill (four tabs, each a `.pane` in `app/webapp/static/index.html`), opened by one vendored `home-head` page header: the tab's title, a context line, the freshness readout that opens the Refresh menu, the theme toggle and the Settings gear (Settings is never a tab). Every sheet (session, context, requests, Settings, extra storage roots, models) is the vendored `modal`, the Settings pane is the vendored `settings-group`, form fields reach the 44px floor in `styles.css` (an input has no `::before` to grow), every tappable row is the vendored `action-row` drawn by `rows.js`, and the toast and the Refresh menu are the vendored `toast` and `row-menu`. The period control and the focus bar sit under the header and apply to every tab. The installed PWA uses the window as its scroller; `index.html` carries the nav's standalone head (`viewport-fit=auto`, `black-translucent`). Never re-author a vendored component; re-vendor it.

**App icons — self-contained, no `brand_gen`.** The PWA icon family (`icon-180/192/512`, distinct `icon-512-maskable`, multi-size `favicon.ico`) + `app/webapp/static/manifest.webmanifest` come from `scripts/gen_icons.py`, a standalone Pillow-only script reproducing `app/webapp/static/favicon.svg` (the Lucide `chart-column` glyph, `#F0F0F0` stroke 2.6 on a `#0A0A0A` tile, no accent colour). The tray loads the generated `icon-512.png`, so web and tray icons share that one definition. It must not import project-scaffolding's `brand_gen`: this repo is public and may not reference other local repos. The generated assets are committed; regenerate after a brand change with `.venv\Scripts\python.exe scripts\gen_icons.py`. (`design_lint`'s app-icon-family check keys on the `brand_gen` contract name + a `render_set()` call in `scripts/`, which this script deliberately lacks, so the finding is an accepted exception.)
