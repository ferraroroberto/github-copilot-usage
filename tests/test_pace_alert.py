"""The Telegram pace alert over a stubbed Telegram API: no network, state in tmp_path."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src import pace_alert


def _budget(**over):
    b = {
        "cycle_start": "2026-11-01",
        "cycle_end": "2026-11-30",
        "days_elapsed": 15,
        "days_total": 30,
        "allowance_credits": 300,
        "used_credits": 160.0,
        "used_pct": 53.3,
        "projected_credits": 320.0,
        "projected_pct": 106.7,
    }
    b.update(over)
    return b


@pytest.fixture
def channel(monkeypatch, tmp_path):
    monkeypatch.setenv(pace_alert.TOKEN_ENV, "123:abc")
    monkeypatch.setenv(pace_alert.CHAT_ENV, "42")
    monkeypatch.setenv("GITHUB_COPILOT_USAGE_DATA_DIR", str(tmp_path))
    return tmp_path


class Telegram:
    """A stand-in Telegram API that records each sendMessage body."""

    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {"ok": True}
        self.sent = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        return httpx.Response(self.status, json=self.body)

    @property
    def transport(self):
        return httpx.MockTransport(self)


def _check(budget, api):
    return asyncio.run(pace_alert.check(budget, transport=api.transport))


@pytest.mark.parametrize("budget, alerted, expected", [
    (_budget(), None, True),
    (_budget(projected_pct=99.9), None, False),
    (_budget(projected_pct=None), None, False),
    (_budget(), "2026-11-01", False),
    (_budget(), "2026-10-01", True),
    (_budget(days_elapsed=2), None, False),
    (_budget(days_elapsed=1, used_pct=100.0, projected_pct=3000.0), None, True),
])
def test_should_alert(budget, alerted, expected):
    assert pace_alert.should_alert(budget, alerted) is expected


def test_sends_once_per_cycle(channel):
    api = Telegram()
    assert _check(_budget(), api) is True
    assert _check(_budget(projected_pct=120.0), api) is False
    assert len(api.sent) == 1
    assert api.sent[0]["chat_id"] == "42"
    assert "106.7%" in api.sent[0]["text"] and "300 credits" in api.sent[0]["text"]

    nxt = _budget(cycle_start="2026-12-01", cycle_end="2026-12-31")
    assert _check(nxt, api) is True
    assert len(api.sent) == 2


def test_failed_send_is_retried_next_check(channel):
    refused = Telegram(status=400, body={"ok": False, "description": "Bad Request: chat not found"})
    with pytest.raises(RuntimeError, match="chat not found"):
        _check(_budget(), refused)
    assert pace_alert.load_alerted_cycle() is None

    assert _check(_budget(), Telegram()) is True
    assert pace_alert.load_alerted_cycle() == "2026-11-01"


def test_network_error_does_not_leak_the_token(channel):
    def boom(request):
        raise httpx.ConnectError("failed for " + str(request.url))

    with pytest.raises(RuntimeError) as err:
        asyncio.run(pace_alert.send_telegram("hi", transport=httpx.MockTransport(boom)))
    assert "123:abc" not in str(err.value)


def test_not_configured_sends_nothing(channel, monkeypatch):
    monkeypatch.setenv(pace_alert.CHAT_ENV, "")
    assert pace_alert.CHAT_ENV in pace_alert.channel_reason()
    api = Telegram()
    assert _check(_budget(), api) is False
    assert api.sent == []
    with pytest.raises(RuntimeError, match="not set"):
        asyncio.run(pace_alert.send_telegram("hi", transport=api.transport))


def test_unreadable_state_counts_as_not_alerted(channel):
    (channel / pace_alert.STATE_FILENAME).write_text("{not json", encoding="utf-8")
    assert pace_alert.load_alerted_cycle() is None
