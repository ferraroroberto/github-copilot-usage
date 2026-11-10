"""get_daily_credits over stubbed GitHub calls: no network, an injected clock."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from src import billing


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.github.com/x")
    return httpx.HTTPStatusError("status", request=request, response=httpx.Response(status, request=request))


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock(datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(billing, "_utcnow", c)
    monkeypatch.setenv(billing.PAT_ENV, "token")
    monkeypatch.setattr(billing, "_day_cache", {})
    monkeypatch.setattr(billing, "_username_cache", None)
    monkeypatch.setattr(billing, "_unavailable", None)
    monkeypatch.setattr(billing, "_get_client", lambda: object())

    async def fake_user(client, pat):
        return "someone"

    monkeypatch.setattr(billing, "_resolve_username", fake_user)
    return c


def _run(days=3):
    return asyncio.run(billing.get_daily_credits(days))


def _stub_days(monkeypatch, per_day):
    calls = []

    async def fake_fetch(client, pat, username, d):
        calls.append(d)
        outcome = per_day(d)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(billing, "_fetch_day", fake_fetch)
    return calls


def test_no_token_is_not_configured(monkeypatch):
    monkeypatch.delenv(billing.PAT_ENV, raising=False)
    body = _run()
    assert body["state"] == "not_configured"
    assert body["available"] is False
    assert "no PAT configured" in body["reason"]
    assert body["daily"] == []
    assert billing.pat_configured() is False


def test_all_days_refused_is_not_an_empty_ready_window(clock, monkeypatch):
    _stub_days(monkeypatch, lambda d: _http_error(403))
    body = _run()
    assert body["state"] == "unavailable"
    assert body["reason_kind"] == "rejected"
    assert body["available"] is False
    assert body["daily"] == []
    assert "403" in body["reason"]


def test_all_network_errors_are_unknown(clock, monkeypatch):
    _stub_days(monkeypatch, lambda d: httpx.ConnectError("down"))
    body = _run()
    assert body["state"] == "unknown"
    assert body["available"] is False
    assert body["reason"]


def test_a_failed_day_among_successes_is_partial(clock, monkeypatch):
    bad = date(2026, 11, 9)
    item = [{"model": "m", "netAmount": 2.0}]
    _stub_days(monkeypatch, lambda d: httpx.ReadTimeout("slow") if d == bad else item)
    body = _run()
    assert body["state"] == "partial"
    assert body["available"] is True
    assert body["failed_days"] == [bad.isoformat()]
    assert {r["date"] for r in body["daily"]} == {"2026-11-08", "2026-11-10"}


def test_every_day_answered_with_no_spend_is_ready(clock, monkeypatch):
    _stub_days(monkeypatch, lambda d: [])
    body = _run()
    assert body["state"] == "ready"
    assert body["available"] is True
    assert body["reason"] is None
    assert body["failed_days"] == []
    assert body["daily"] == []


def test_a_404_is_not_on_enhanced_billing_and_is_held(clock, monkeypatch):
    calls = _stub_days(monkeypatch, lambda d: _http_error(404))
    body = _run()
    assert body["state"] == "unavailable"
    assert body["reason_kind"] == "not_on_enhanced_billing"
    asked = len(calls)
    assert _run()["state"] == "unavailable"
    assert len(calls) == asked  # held for the TTL, not re-asked
    clock.now += timedelta(seconds=billing._UNAVAILABLE_TTL_SECS + 1)
    _run()
    assert len(calls) > asked


def test_rejected_username_is_unavailable_and_network_error_is_not_held(clock, monkeypatch):
    results = [_http_error(401), httpx.ConnectError("down")]

    async def fake_user(client, pat):
        raise results.pop(0)

    monkeypatch.setattr(billing, "_resolve_username", fake_user)
    first = _run()
    assert first["state"] == "unavailable" and first["reason_kind"] == "rejected"

    monkeypatch.setattr(billing, "_unavailable", None)
    second = _run()
    assert second["state"] == "unknown"
    assert billing._unavailable is None  # a transient error is retried, not held


def test_a_day_fetched_while_accruing_is_refetched_once_after_midnight(clock, monkeypatch):
    clock.now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    amounts = {"v": 1.0}
    calls = _stub_days(monkeypatch, lambda d: [{"model": "m", "netAmount": amounts["v"]}])
    _run(days=2)
    nov10 = date(2026, 11, 10)
    assert calls.count(nov10) == 1

    clock.now = datetime(2026, 11, 11, 0, 30, tzinfo=timezone.utc)  # Nov 10 has ended
    amounts["v"] = 5.0  # the day's final figure
    body = _run(days=2)
    assert calls.count(nov10) == 2
    final = [r for r in body["daily"] if r["date"] == "2026-11-10"]
    assert final[0]["credits"] == 5.0

    clock.now = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
    _run(days=3)
    assert calls.count(nov10) == 2  # final now: never fetched again


def test_failed_response_is_unknown_with_the_reason():
    body = billing.failed_response(RuntimeError("boom"))
    assert body["state"] == "unknown"
    assert body["reason"] == "boom"
    assert body["available"] is False
    assert body["daily"] == []
