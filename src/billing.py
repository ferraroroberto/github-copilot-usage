"""Optional: official per-day AI-credit spend from the GitHub billing API.

The local session parsers only see usage this machine wrote to disk. This
module is the authoritative counterpart: GitHub's billing API reports the
real per-day x per-model credit spend for the *account*, across every device.
Trade-off: no session/project attribution, so the dashboard shows it as its
own "official" card next to the local breakdown.

Auth: a fine-grained GitHub PAT with the **"Plan" read-only** user permission
in ``GITHUB_COPILOT_BILLING_PAT`` (via ``.env`` or the environment). Unset
PAT, a 404 (account not on the enhanced billing platform), or any HTTP error
degrade to ``{"available": False, "reason": ...}`` — the dashboard renders
fine with zero billing configured.

Cached, not hammered: past days are immutable and cached forever; only
"today" (still accruing) is refreshed, at most every 5 minutes.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is in requirements.txt
    httpx = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

PAT_ENV = "GITHUB_COPILOT_BILLING_PAT"
_API_BASE = "https://api.github.com"
_TODAY_REFRESH_SECS = 300
_UNAVAILABLE_TTL_SECS = 300

_username_cache: Optional[str] = None
_day_cache: Dict[date, Dict[str, Any]] = {}
_unavailable: Optional[Dict[str, Any]] = None
_client: Optional["httpx.AsyncClient"] = None


def _get_client() -> "httpx.AsyncClient":
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _headers(pat: str) -> Dict[str, str]:
    return {
        "Authorization": "Bearer " + pat,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _degraded(reason: str) -> Dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "daily": [],
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
    }


def _set_unavailable(reason: str) -> str:
    global _unavailable
    _unavailable = {"reason": reason, "at": time.time()}
    return reason


def _still_unavailable() -> Optional[str]:
    if _unavailable is None:
        return None
    if time.time() - _unavailable["at"] > _UNAVAILABLE_TTL_SECS:
        return None
    return _unavailable["reason"]


async def _resolve_username(client: "httpx.AsyncClient", pat: str) -> str:
    resp = await client.get(_API_BASE + "/user", headers=_headers(pat), timeout=10.0)
    resp.raise_for_status()
    login = (resp.json() or {}).get("login")
    if not login:
        raise ValueError("GitHub /user response had no 'login'")
    return login


async def _fetch_day(
    client: "httpx.AsyncClient", pat: str, username: str, d: date
) -> List[Dict[str, Any]]:
    resp = await client.get(
        _API_BASE + "/users/" + username + "/settings/billing/ai_credit/usage",
        headers=_headers(pat),
        params={"year": d.year, "month": d.month, "day": d.day},
        timeout=15.0,
    )
    resp.raise_for_status()
    body = resp.json() or {}
    items = body.get("usageItems")
    return items if isinstance(items, list) else []


def _aggregate_day(d: date, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_model: Dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        model = item.get("model") or item.get("sku") or "unknown"
        try:
            by_model[model] = by_model.get(model, 0.0) + float(item.get("netAmount") or 0.0)
        except (TypeError, ValueError):
            continue
    return [
        {"date": d.isoformat(), "model": model, "credits": round(credits, 4),
         "usd": round(credits * 0.01, 4)}
        for model, credits in by_model.items()
    ]


async def get_daily_credits(days: int = 14) -> Dict[str, Any]:
    """Per-day x per-model credit spend for the last ``days`` days (UTC)."""
    pat = os.environ.get(PAT_ENV, "").strip()
    if not pat:
        return _degraded("no PAT configured — set " + PAT_ENV + " in .env to enable")
    if httpx is None:
        return _degraded("httpx not installed")

    sticky = _still_unavailable()
    if sticky is not None:
        return _degraded(sticky)

    client = _get_client()

    global _username_cache
    if _username_cache is None:
        try:
            _username_cache = await _resolve_username(client, pat)
        except (httpx.HTTPError, ValueError) as exc:
            reason = "could not resolve GitHub username: " + str(exc)
            _log.warning("⚠️ billing: %s", reason)
            return _degraded(_set_unavailable(reason))

    today = datetime.now(tz=timezone.utc).date()
    window = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]

    rows: List[Dict[str, Any]] = []
    for d in window:
        cached = _day_cache.get(d)
        stale = cached is not None and d == today and (
            time.time() - cached["fetched_at"] > _TODAY_REFRESH_SECS
        )
        if cached is None or stale:
            try:
                items = await _fetch_day(client, pat, _username_cache, d)
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    reason = ("GitHub billing API returned 404 "
                              "(account likely not on the enhanced billing platform)")
                    _log.info("ℹ️ billing: %s", reason)
                    return _degraded(_set_unavailable(reason))
                _log.warning("⚠️ billing: %s failed: %s", d, exc)
                continue
            except httpx.HTTPError as exc:
                _log.warning("⚠️ billing: network error fetching %s: %s", d, exc)
                continue
            _day_cache[d] = {"items": items, "fetched_at": time.time()}
            cached = _day_cache[d]
        rows.extend(_aggregate_day(d, cached["items"]))

    return {
        "available": True,
        "reason": None,
        "daily": rows,
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
    }
