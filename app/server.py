"""FastAPI app: JSON API + the static dashboard.

Binds to 127.0.0.1 only — this is a personal, local tool; nothing is exposed
on the network. All endpoints are read-only over Copilot's own log files
except POST /api/config (writes config.json) and POST /api/scan (re-runs
discovery, still read-only on disk).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import aggregate, billing, config
from app.version import VERSION

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="github-copilot-usage", version=VERSION, docs_url="/api/docs")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await billing.close_client()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": VERSION}


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
        return {"available": False, "reason": str(exc), "daily": [], "as_of": None}


@app.get("/api/config")
async def api_config_get() -> dict:
    cfg = config.load()
    cfg["billing_pat_configured"] = bool(os.environ.get(billing.PAT_ENV, "").strip())
    return cfg


@app.post("/api/config")
async def api_config_post(updates: dict) -> dict:
    return config.save(updates or {})


# ---------------------------------------------------------------------------
# Static dashboard
# ---------------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="static")
