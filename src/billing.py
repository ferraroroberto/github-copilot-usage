"""Optional: official per-day AI-credit spend from the GitHub billing API.

The local session parsers only see usage this machine wrote to disk. This
module is the authoritative counterpart: GitHub's billing API reports the
real per-day x per-model credit spend for the *account*, across every device.
Trade-off: no session/project attribution, so the dashboard shows it as its
own "official" card next to the local breakdown.

Auth: a fine-grained GitHub PAT with the **"Plan" read-only** user permission
in ``GITHUB_COPILOT_BILLING_PAT`` (via ``.env`` or the environment). This
module only *gathers* what happened (``Observation``); ``src/billing_state.py``
decides what it means (not configured / unavailable / ready / partial /
unknown). The dashboard renders fine with zero billing configured.

Cached, not hammered: a day is cached once it is *final*, meaning it was
fetched after its UTC day ended. A day fetched while it was still accruing is
refreshed at most every 5 minutes and re-fetched once after midnight.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is in requirements.txt
    httpx = None  # type: ignore[assignment]

from src.aggregate import USD_PER_CREDIT
from src.billing_state import (
    PAT_ENV,
    BillingState,
    BillingStatus,
    DayOutcome,
    Observation,
    Outcome,
    derive,
)

_log = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
_TODAY_REFRESH_SECS = 300
_UNAVAILABLE_TTL_SECS = 300

_username_cache: Optional[str] = None
# date -> {"items": [...], "fetched_at": aware UTC datetime}
_day_cache: Dict[date, Dict[str, Any]] = {}
# The last UNAVAILABLE verdict and when it was reached, so a refused token is not re-asked
# on every poll. Only a definite "no" is held; UNKNOWN is always retried.
_unavailable: Optional[Tuple[BillingStatus, datetime]] = None
_logged_state: Optional[BillingState] = None
_client: Optional["httpx.AsyncClient"] = None


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


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


def _read_pat() -> str:
    return os.environ.get(PAT_ENV, "").strip()


def pat_configured() -> bool:
    """Whether a token is set, read from the same derivation as the card."""
    status = derive(Observation(pat_present=bool(_read_pat()), httpx_present=httpx is not None))
    return status.state is not BillingState.NOT_CONFIGURED


def _response(status: BillingStatus, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The /api/billing body. ``available`` is derived for older clients."""
    global _logged_state
    if status.state is not _logged_state:
        _log.info("ℹ️ billing: state %s (%s)", status.state.value, status.reason or "ok")
        _logged_state = status.state
    return {
        "state": status.state.value,
        "available": status.available,
        "reason": status.reason,
        "reason_kind": status.kind.value if status.kind is not None else None,
        "failed_days": list(status.failed_days),
        "daily": rows,
        "as_of": _utcnow().isoformat(),
    }


def failed_response(exc: Exception) -> Dict[str, Any]:
    """The body for an unexpected exception: the card could not tell."""
    return _response(derive(Observation(internal_error=str(exc))), [])


def _held_unavailable() -> Optional[BillingStatus]:
    if _unavailable is None:
        return None
    status, at = _unavailable
    if (_utcnow() - at).total_seconds() > _UNAVAILABLE_TTL_SECS:
        return None
    return status


def _outcome_of(exc: Exception) -> Outcome:
    if isinstance(exc, httpx.HTTPStatusError):
        return Outcome(ok=False, status=exc.response.status_code,
                       detail="HTTP %d" % exc.response.status_code)
    if isinstance(exc, ValueError):
        return Outcome(ok=False, detail=str(exc), malformed=True)
    return Outcome(ok=False, detail=type(exc).__name__ + ": " + str(exc))


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
         "usd": round(credits * USD_PER_CREDIT, 4)}
        for model, credits in by_model.items()
    ]


def _needs_fetch(d: date, now: datetime) -> bool:
    """A day is final once it was fetched after its UTC day ended; until then it is refetched."""
    cached = _day_cache.get(d)
    if cached is None:
        return True
    fetched_at = cached["fetched_at"]
    if fetched_at.date() > d:
        return False
    if d < now.date():
        return True  # fetched while still accruing, and the day has since ended: once more
    return (now - fetched_at).total_seconds() > _TODAY_REFRESH_SECS


async def get_daily_credits(days: int = 14) -> Dict[str, Any]:
    """Per-day x per-model credit spend for the last ``days`` days (UTC)."""
    global _username_cache, _unavailable
    pat = _read_pat()
    base = Observation(pat_present=bool(pat), httpx_present=httpx is not None)
    unconfigured = derive(base)
    if unconfigured.state in (BillingState.NOT_CONFIGURED, BillingState.UNAVAILABLE):
        return _response(unconfigured, [])

    held = _held_unavailable()
    if held is not None:
        return _response(held, [])

    client = _get_client()

    if _username_cache is None:
        try:
            _username_cache = await _resolve_username(client, pat)
        except (httpx.HTTPError, ValueError) as exc:
            _log.warning("⚠️ billing: username lookup failed: %s", exc)
            status = derive(Observation(pat_present=True, username=_outcome_of(exc)))
            if status.state is BillingState.UNAVAILABLE:
                _unavailable = (status, _utcnow())
            return _response(status, [])

    now = _utcnow()
    today = now.date()
    window = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]

    rows: List[Dict[str, Any]] = []
    outcomes: List[DayOutcome] = []
    for d in window:
        if _needs_fetch(d, now):
            try:
                items = await _fetch_day(client, pat, _username_cache, d)
            except (httpx.HTTPError, ValueError) as exc:
                outcome = _outcome_of(exc)
                _log.warning("⚠️ billing: fetching %s failed: %s", d, outcome.detail)
                outcomes.append(DayOutcome(d, outcome))
                if outcome.status == 404:
                    break  # the account has no such endpoint; the rest would say the same
                continue
            _day_cache[d] = {"items": items, "fetched_at": _utcnow()}
        outcomes.append(DayOutcome(d, Outcome(ok=True)))
        rows.extend(_aggregate_day(d, _day_cache[d]["items"]))

    status = derive(Observation(pat_present=True, username=Outcome(ok=True), days=tuple(outcomes)))
    if status.state is BillingState.UNAVAILABLE:
        _unavailable = (status, _utcnow())
        rows = []
    return _response(status, rows)
