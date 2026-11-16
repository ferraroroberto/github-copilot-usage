"""Shared synthetic-record factory for the insight tests (no real logs, prompts or figures)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Callable

import pytest

from src.vscode_parser import RequestRecord

# Noon local time: a day-bucketed period test cannot straddle midnight.
TODAY_NOON = datetime.combine(date.today(), time(12, 0)).astimezone()


@pytest.fixture
def tools_heavy() -> list:
    """A composition where tool definitions dominate and the user's own text is 10%."""
    return [
        {"category": "System", "label": "System Instructions", "percentageOfPrompt": 20},
        {"category": "System", "label": "Tool Definitions", "percentageOfPrompt": 70},
        {"category": "User Context", "label": "Messages", "percentageOfPrompt": 10},
    ]


@pytest.fixture
def rec() -> Callable[..., RequestRecord]:
    """``rec(n, **overrides)``: request ``n`` of a synthetic session, one minute apart, today."""

    def make(n: int = 0, days_ago: int = 0, **kw) -> RequestRecord:
        base = dict(
            ide="Code",
            source="vscode",
            project_key="e:\\alpha",
            project_name="alpha",
            project_path="E:\\alpha",
            session_id="sess-a",
            session_title="Alpha session",
            request_id="req-%03d" % n,
            ts=TODAY_NOON - timedelta(days=days_ago) + timedelta(minutes=n),
            message="prompt number %d" % n,
            mode="agent",
            model_requested="copilot/gpt-5-mini",
            model="gpt-5-mini",
            prompt_tokens=10_000,
            completion_tokens=100,
            credits=1.0,
            elapsed_ms=1000,
        )
        base.update(kw)
        return RequestRecord(**base)

    return make
