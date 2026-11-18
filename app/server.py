"""FastAPI app: JSON API + the static dashboard.

This is a personal, local tool: app/listeners.py serves it on 127.0.0.1, and on the machine's Tailscale
address only when the owner turns that on; it is never bound to 0.0.0.0. All endpoints are read-only over
Copilot's own log files except POST /api/config, PUT /api/sessions/{id}/name, PUT /api/features/{id} and
PUT /api/tailnet (all write config.json); the
refresh and cache-clear endpoints only change this process's parsed state and its read model in the
runtime-data folder.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import PlainTextResponse, Response

from app import build_info
from app.version import VERSION
from src import (
    aggregate,
    billing,
    config,
    context_drilldown,
    features,
    index_queries,
    insights,
    model_labels,
    pace_alert,
    read_model,
    refresh,
    request_explorer,
    tailnet,
)
from src.shell_etag import shell_response
from src.static_versioning import CachingStaticFiles, StaticVersion
from src.usage_store import usage_db_path
from src.vscode_parser import RequestRecord

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "webapp" / "static"
# Hashed once at import: the app restarts on every code edit, so the stamp never goes stale.
STATIC_VERSION = StaticVersion(STATIC_DIR)

app = FastAPI(title="github-copilot-usage", version=VERSION, docs_url="/api/docs")
# A phone on a slow link pays for every byte of the shell, the module graph and the JSON.
app.add_middleware(GZipMiddleware, minimum_size=500)

def _requires(block_id: str):
    """A route dependency that refuses the call while a feature block is switched off."""

    async def check() -> None:
        if not config.feature_enabled(block_id):
            raise HTTPException(status_code=404, detail="turned off in Settings, Available features: " + block_id)

    return Depends(check)


# Local-only view/tap log: the vendored router over a per-user database. Resolving the path creates
# nothing, so an unwritable data folder cannot stop the server from starting.
# Mounted before the catch-all static mount below. The vendored module targets Python 3.11+ while
# this app promises 3.9+, so a failed import drops only the log: the dashboard still starts.
try:
    from src.usage_log import usage_router
except Exception as exc:  # any failure of this optional component must not stop the server
    _log.warning("⚠️ usage log disabled, the dashboard runs without it: %s: %s", type(exc).__name__, exc)
else:
    app.include_router(usage_router(usage_db_path()), dependencies=[_requires("usage_log")])


async def _pace_alert_loop() -> None:
    """Check the cycle projection on a timer; a failed check is logged and retried next tick."""
    while True:
        try:
            if pace_alert.channel_reason() is None:
                await pace_alert.check(aggregate.budget_status(await _records()))
        except Exception as exc:  # the optional alert must never stop the loop or the server
            _log.warning("⚠️ pace alert check failed: %s", exc)
        await asyncio.sleep(pace_alert.CHECK_INTERVAL_SECS)


# Held here so the event loop's weak reference is not the task's only one.
_pace_task: Optional["asyncio.Task[None]"] = None


def _sync_pace_alert() -> None:
    """Run the pace-alert timer exactly while its feature block is on."""
    global _pace_task
    running = _pace_task is not None and not _pace_task.done()
    if config.feature_enabled("pace_alert"):
        if not running:
            _pace_task = asyncio.create_task(_pace_alert_loop())
    elif _pace_task is not None:
        _pace_task.cancel()
        _pace_task = None


@app.on_event("startup")
async def _startup() -> None:
    reason = pace_alert.channel_reason()
    if reason is not None:
        _log.info("ℹ️ pace alert off: Telegram %s", reason)
    _sync_pace_alert()


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _pace_task is not None:
        _pace_task.cancel()
    await billing.close_client()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def _records() -> List[RequestRecord]:
    """The parsed records as the dashboard may see them: after the start date, sessions renamed."""
    return aggregate.visible((await refresh.store.snapshot()).records)


def _summary_of(records: List[RequestRecord], period: str) -> dict:
    return aggregate.get_summary(aggregate.visible(records), period)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": VERSION}


@app.get("/api/version")
async def api_version() -> dict:
    return {"version": VERSION, "git_sha": build_info.GIT_SHA, "built_at": build_info.BUILT_AT}


@app.get("/api/summary")
async def api_summary(
    period: str = Query("today", description="today | week | month | cycle | all | range:YYYY-MM-DD..YYYY-MM-DD"),
) -> dict:
    snap = await refresh.store.snapshot()
    body = refresh.store.summary(snap, period, _summary_of)
    body["sources"] = snap.sources
    return body


@app.get("/api/trends")
async def api_trends(
    period: str = Query("week", description="today | week | month | cycle | all | range:YYYY-MM-DD..YYYY-MM-DD"),
    offset: int = Query(0, ge=0, le=520, description="how many whole windows before the latest"),
) -> dict:
    return aggregate.get_trends(await _records(), period, offset)


@app.get("/api/sessions")
async def api_sessions(
    period: str = Query("all", description="today | week | month | cycle | all | range:YYYY-MM-DD..YYYY-MM-DD"),
) -> dict:
    snap = await refresh.store.snapshot()
    with read_model.model.reading(snap.index) as conn:
        rows = index_queries.sessions(conn, snap.index, period) if conn is not None else None
    records = aggregate.visible(snap.records)
    return {"period": period, "sessions": aggregate.get_sessions(records, period, rows)}


@app.get("/api/sessions/{session_id}")
async def api_session_detail(session_id: str) -> dict:
    detail = aggregate.get_session_detail(await _records(), session_id)
    if detail is None:
        return {"error": "session not found", "session_id": session_id}
    return detail


@app.put("/api/sessions/{session_id}/name")
async def api_session_name(session_id: str, body: dict) -> dict:
    """Set a session's custom name, or clear it (empty or null) to restore the parsed title."""
    try:
        name = config.clean_session_name((body or {}).get("name"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="name: %s" % exc)
    names = dict(config.load().get("session_names") or {})
    if name:
        if not any(r.session_id == session_id for r in await _records()):
            raise HTTPException(status_code=404, detail="session not found")
        names[session_id] = name
    else:
        names.pop(session_id, None)
    try:
        config.save({"session_names": names})
    except config.ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _log.info("ℹ️ session %s: %s", session_id[:8], "renamed" if name else "name cleared")
    return {"session_id": session_id, "name": name or None}


@app.get("/api/requests")
async def api_requests(
    period: str = Query("week", description="today | week | month | cycle | all"),
    project: str = Query("", description="project key from the facets"),
    model: str = Query(""),
    mode: str = Query(""),
    ide: str = Query(""),
    result: str = Query("", description="ok | error"),
    min_credits: Optional[float] = Query(None, ge=0, description="billed requests only"),
    max_credits: Optional[float] = Query(None, ge=0, description="billed requests only"),
    q: str = Query("", description="text in the prompt preview or the session title"),
    sort: str = Query("ts", description="ts | credits | tokens | elapsed"),
    order: str = Query("desc", description="asc | desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(request_explorer.DEFAULT_PAGE_SIZE, ge=1, le=request_explorer.MAX_PAGE_SIZE),
) -> dict:
    query = request_explorer.Query.build(
        period=period, project=project, model=model, mode=mode, ide=ide, result=result,
        min_credits=min_credits, max_credits=max_credits, q=q, sort=sort, order=order,
        page=page, page_size=page_size,
    )
    snap = await refresh.store.snapshot()
    with read_model.model.reading(snap.index) as conn:
        if conn is not None:
            return index_queries.explore(conn, snap.index, query)
    return request_explorer.explore(aggregate.visible(snap.records), query)


@app.get("/api/insights")
async def api_insights(
    period: str = Query("week", description="today | week | month | cycle | all"),
) -> dict:
    return insights.build_insights(await _records(), period)


@app.get("/api/context")
async def api_context(
    period: str = Query("week", description="today | week | month | cycle | all"),
    label: str = Query("", description="a prompt-composition label; empty means the whole prompt"),
) -> dict:
    return context_drilldown.drilldown(await _records(), period, label)


@app.get("/api/export.csv")
async def api_export(
    period: str = Query("all", description="today | week | month | cycle | all"),
) -> PlainTextResponse:
    period = aggregate.normalize_period(period, "all")
    csv_text = aggregate.export_csv(await _records(), period)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition":
                'attachment; filename="copilot-usage-' + re.sub(r"[^A-Za-z0-9-]+", "_", period) + '.csv"'
        },
    )


@app.get("/api/billing", dependencies=[_requires("billing_card")])
async def api_billing(days: int = Query(14, ge=1, le=30)) -> dict:
    try:
        return await billing.get_daily_credits(days)
    except Exception as exc:  # never let the optional card break the page
        _log.warning("⚠️ billing endpoint error: %s", exc, exc_info=True)
        return billing.failed_response(exc)


@app.post("/api/refresh")
async def api_refresh(force: bool = Query(False, description="drop every parsed record and the read model, then rebuild everything")) -> dict:
    """Rebuild the snapshot now, or join the rebuild in flight; the body says which."""
    try:
        return refresh.describe(await refresh.store.refresh(force))
    except Exception as exc:  # the reason goes back to the toast
        _log.warning("⚠️ refresh failed: %s", exc, exc_info=True)
        return {"ok": False, "reason": type(exc).__name__ + ": " + str(exc)}


@app.post("/api/cache/clear")
async def api_cache_clear() -> dict:
    """Drop the snapshot, the cached summaries and every parsed record; the next read rebuilds."""
    await refresh.store.clear()
    return {"ok": True}


@app.get("/api/config")
async def api_config_get() -> dict:
    cfg = config.load()
    cfg["billing_pat_configured"] = billing.pat_configured()
    cfg["pace_alert_reason"] = pace_alert.channel_reason()
    return cfg


@app.post("/api/config")
async def api_config_post(updates: dict) -> dict:
    try:
        return config.save(updates or {})
    except config.ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/features")
async def api_features_get() -> dict:
    """The feature blocks with their switch and whether this machine can run each."""
    return {"blocks": features.describe()}


@app.put("/api/features/{block_id}")
async def api_features_put(block_id: str, body: dict) -> dict:
    """Switch one block on or off; takes effect now, with no restart."""
    enabled = (body or {}).get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled: must be true or false")
    try:
        features.set_enabled(block_id, enabled)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown feature block")
    except config.ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _sync_pace_alert()
    return {"blocks": features.describe()}


@app.get("/api/tailnet")
async def api_tailnet_get() -> dict:
    """The tailnet option: its switch, whether this server is serving it, the URL, or why not."""
    return tailnet.describe()


@app.put("/api/tailnet")
async def api_tailnet_put(body: dict) -> dict:
    """Switch the tailnet option; the bind is made at server start, so it applies after a restart."""
    enabled = (body or {}).get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled: must be true or false")
    try:
        config.save({"tailnet_access": enabled})
    except config.ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _log.info("ℹ️ tailnet access: %s in config, applies at the next server start", "on" if enabled else "off")
    return tailnet.describe()


@app.get("/api/models")
async def api_models() -> dict:
    """Every model in the local history as spelled before any alias, for the Settings choices.

    Read before the start date applies, so an alias stays editable whatever date is set.
    """
    records = (await refresh.store.snapshot()).records
    return {"families": model_labels.family_options(), "models": aggregate.labels_for(records).catalog()}


@app.post("/api/alerts/test", dependencies=[_requires("pace_alert")])
async def api_alert_test() -> dict:
    try:
        await pace_alert.send_telegram("Test from github-copilot-usage: pace alerts will arrive in this chat.")
    except Exception as exc:  # the reason goes back to the settings dialog
        _log.warning("⚠️ pace alert test failed: %s", exc)
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "reason": None}


# ---------------------------------------------------------------------------
# Static dashboard
# ---------------------------------------------------------------------------


@app.get("/")
async def index(request: Request) -> Response:
    return shell_response(request, STATIC_VERSION.shell_html())


app.mount("/", CachingStaticFiles(directory=STATIC_DIR, version=STATIC_VERSION), name="static")
