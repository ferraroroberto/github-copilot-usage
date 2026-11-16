"""FastAPI app: JSON API + the static dashboard.

Binds to 127.0.0.1 only — this is a personal, local tool; nothing is exposed
on the network. All endpoints are read-only over Copilot's own log files
except POST /api/config (writes config.json).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import PlainTextResponse, Response

from app import build_info
from app.version import VERSION
from src import (
    aggregate,
    billing,
    config,
    context_drilldown,
    insights,
    model_labels,
    pace_alert,
    request_explorer,
)
from src.static_versioning import CachingStaticFiles, StaticVersion
from src.usage_store import usage_db_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "webapp" / "static"
# Hashed once at import: the app restarts on every code edit, so the stamp never goes stale.
STATIC_VERSION = StaticVersion(STATIC_DIR)

app = FastAPI(title="github-copilot-usage", version=VERSION, docs_url="/api/docs")
# A phone on a slow link pays for every byte of the shell, the module graph and the JSON.
app.add_middleware(GZipMiddleware, minimum_size=500)

# Local-only view/tap log: the vendored router over a per-user database. Resolving the path creates
# nothing, so an unwritable data folder cannot stop the server from starting.
# Mounted before the catch-all static mount below. The vendored module targets Python 3.11+ while
# this app promises 3.9+, so a failed import drops only the log: the dashboard still starts.
try:
    from src.usage_log import usage_router
except Exception as exc:  # any failure of this optional component must not stop the server
    _log.warning("⚠️ usage log disabled, the dashboard runs without it: %s: %s", type(exc).__name__, exc)
else:
    app.include_router(usage_router(usage_db_path()))


async def _pace_alert_loop() -> None:
    """Check the cycle projection on a timer; a failed check is logged and retried next tick."""
    while True:
        try:
            if pace_alert.channel_reason() is None:
                records, _ = aggregate.gather_records()
                await pace_alert.check(aggregate.budget_status(records))
        except Exception as exc:  # the optional alert must never stop the loop or the server
            _log.warning("⚠️ pace alert check failed: %s", exc)
        await asyncio.sleep(pace_alert.CHECK_INTERVAL_SECS)


# Held here so the event loop's weak reference is not the task's only one.
_background: list = []


@app.on_event("startup")
async def _startup() -> None:
    reason = pace_alert.channel_reason()
    if reason is not None:
        _log.info("ℹ️ pace alert off: Telegram %s", reason)
    _background.append(asyncio.create_task(_pace_alert_loop()))


@app.on_event("shutdown")
async def _shutdown() -> None:
    for task in _background:
        task.cancel()
    await billing.close_client()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": VERSION}


@app.get("/api/version")
async def api_version() -> dict:
    return {"version": VERSION, "git_sha": build_info.GIT_SHA, "built_at": build_info.BUILT_AT}


@app.get("/api/summary")
async def api_summary(
    period: str = Query("today", description="today | week | month | cycle | all"),
) -> dict:
    records, sources = aggregate.gather_records()
    body = aggregate.get_summary(records, period)
    body["sources"] = sources
    return body


@app.get("/api/sessions")
async def api_sessions(
    period: str = Query("all", description="today | week | month | cycle | all"),
) -> dict:
    records, _ = aggregate.gather_records()
    return {"period": period, "sessions": aggregate.get_sessions(records, period)}


@app.get("/api/sessions/{session_id}")
async def api_session_detail(session_id: str) -> dict:
    records, _ = aggregate.gather_records()
    detail = aggregate.get_session_detail(records, session_id)
    if detail is None:
        return {"error": "session not found", "session_id": session_id}
    return detail


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
    records, _ = aggregate.gather_records()
    query = request_explorer.Query.build(
        period=period, project=project, model=model, mode=mode, ide=ide, result=result,
        min_credits=min_credits, max_credits=max_credits, q=q, sort=sort, order=order,
        page=page, page_size=page_size,
    )
    return request_explorer.explore(records, query)


@app.get("/api/insights")
async def api_insights(
    period: str = Query("week", description="today | week | month | cycle | all"),
) -> dict:
    records, _ = aggregate.gather_records()
    return insights.build_insights(records, period)


@app.get("/api/context")
async def api_context(
    period: str = Query("week", description="today | week | month | cycle | all"),
    label: str = Query("", description="a prompt-composition label; empty means the whole prompt"),
) -> dict:
    records, _ = aggregate.gather_records()
    return context_drilldown.drilldown(records, period, label)


@app.get("/api/export.csv")
async def api_export(
    period: str = Query("all", description="today | week | month | cycle | all"),
) -> PlainTextResponse:
    records, _ = aggregate.gather_records()
    csv_text = aggregate.export_csv(records, period)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition":
                'attachment; filename="copilot-usage-' + period + '.csv"'
        },
    )


@app.get("/api/billing")
async def api_billing(days: int = Query(14, ge=1, le=30)) -> dict:
    try:
        return await billing.get_daily_credits(days)
    except Exception as exc:  # never let the optional card break the page
        _log.warning("⚠️ billing endpoint error: %s", exc, exc_info=True)
        return billing.failed_response(exc)


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


@app.get("/api/models")
async def api_models() -> dict:
    """Every model in the local history as spelled before any alias, for the Settings choices."""
    records, _ = aggregate.gather_records()
    return {"families": model_labels.family_options(), "models": aggregate.labels_for(records).catalog()}


@app.post("/api/alerts/test")
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
async def index(if_none_match: Optional[str] = Header(None)) -> Response:
    return STATIC_VERSION.index_response(if_none_match)


app.mount("/", CachingStaticFiles(directory=STATIC_DIR, version=STATIC_VERSION), name="static")
