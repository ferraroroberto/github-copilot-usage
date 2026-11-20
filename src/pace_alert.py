"""Optional Telegram nudge when the billing-cycle projection passes the allowance.

The burn-down on the dashboard is only seen when the page is open. When a
Telegram bot token and chat id are set in ``.env``, the server checks the
cycle budget on a timer and sends one message the first time the linear
projection reaches 100% of the allowance, then stays quiet for the rest of
that cycle. Nothing configured → nothing runs, and the settings dialog says
why.

Which cycle already alerted is kept in a small JSON file in the app's
runtime-data folder (never inside the repo), and is written only after
Telegram accepted the message, so a failed send is retried on the next check.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from src import config
from src.runtime_data import runtime_data_dir

try:
    import httpx
except ImportError:  # httpx is a runtime dep, but the alert must never break the server
    httpx = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

APP = "github-copilot-usage"
STATE_FILENAME = "pace-alert.json"
TOKEN_ENV = "COPILOT_USAGE_TELEGRAM_BOT_TOKEN"
CHAT_ENV = "COPILOT_USAGE_TELEGRAM_CHAT_ID"

# The projection is the allowance this cycle will reach at the current pace.
THRESHOLD_PCT = 100.0
# Day one or two of a cycle extrapolates a single busy morning across a month;
# below this the alert waits unless the allowance is already used up.
MIN_DAYS_ELAPSED = 3
# A stated guess: the budget changes only as fast as you use Copilot, so half an
# hour is prompt enough without re-reading logs needlessly.
CHECK_INTERVAL_SECS = 30 * 60
_TIMEOUT_SECS = 10.0


def _token() -> str:
    config.load()  # .env values must be in the environment before reading it
    return os.environ.get(TOKEN_ENV, "").strip()


def _chat_id() -> str:
    config.load()
    return os.environ.get(CHAT_ENV, "").strip()


def channel_reason() -> Optional[str]:
    """Why the alert cannot send, or None when it can."""
    missing = [name for name, value in ((TOKEN_ENV, _token()), (CHAT_ENV, _chat_id())) if not value]
    if missing:
        return "not set: add " + " and ".join(missing) + " to .env"
    if httpx is None:
        return "httpx not installed"
    return None


def should_alert(budget: Dict[str, Any], alerted_cycle: Optional[str]) -> bool:
    """True when the projection crossed the threshold and this cycle has not alerted yet."""
    projected = budget.get("projected_pct")
    if projected is None or projected < THRESHOLD_PCT:
        return False
    if budget.get("cycle_start") == alerted_cycle:
        return False
    used = budget.get("used_pct") or 0.0
    return budget.get("days_elapsed", 0) >= MIN_DAYS_ELAPSED or used >= THRESHOLD_PCT


def format_message(budget: Dict[str, Any]) -> str:
    return (
        "Copilot credits are on pace to exceed your allowance.\n"
        "Projected: {projected:g} of {allowance} credits ({projected_pct:g}%) by {end}.\n"
        "Used so far: {used:g} ({used_pct:g}%) on day {day} of {days}.\n"
        "Local data from one machine only."
    ).format(
        projected=budget["projected_credits"],
        allowance=budget["allowance_credits"],
        projected_pct=budget["projected_pct"],
        end=budget["cycle_end"],
        used=budget["used_credits"],
        used_pct=budget.get("used_pct") or 0.0,
        day=budget["days_elapsed"],
        days=budget["days_total"],
    )


def state_path() -> Path:
    config.load()  # the data-dir override may come from .env
    return runtime_data_dir(APP) / STATE_FILENAME


def load_alerted_cycle() -> Optional[str]:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _log.warning("⚠️ pace alert: state file unreadable (%s); treating the cycle as not alerted", exc)
        return None
    value = raw.get("alerted_cycle") if isinstance(raw, dict) else None
    return value if isinstance(value, str) else None


def save_alerted_cycle(cycle_start: str) -> None:
    path = runtime_data_dir(APP, create=True) / STATE_FILENAME
    path.write_text(json.dumps({"alerted_cycle": cycle_start}) + "\n", encoding="utf-8")


TEST_TEXT = "TEST ONLY, not a real alert: github-copilot-usage pace alerts will arrive in this chat."


async def send_telegram(text: str, transport: Optional[Any] = None) -> int:
    """Send ``text`` to the configured chat and return Telegram's message id.

    Raises RuntimeError with the reason on any failure, including a 200 that carries no message id,
    so a caller never reports a delivery Telegram did not confirm.
    """
    reason = channel_reason()
    if reason is not None:
        raise RuntimeError("Telegram " + reason)
    url = "https://api.telegram.org/bot" + _token() + "/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECS, transport=transport) as client:
            resp = await client.post(url, json={"chat_id": _chat_id(), "text": text})
    except httpx.HTTPError as exc:
        # The exception text can carry the URL, and the URL carries the token.
        raise RuntimeError("could not reach Telegram (" + type(exc).__name__ + ")") from None
    if resp.status_code != 200:
        try:
            detail = str(resp.json().get("description", ""))
        except ValueError:
            detail = ""
        raise RuntimeError("Telegram refused the message (HTTP %d) %s" % (resp.status_code, detail))
    try:
        message_id = resp.json()["result"]["message_id"]
    except (ValueError, KeyError, TypeError):
        message_id = None
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        raise RuntimeError("Telegram answered without confirming delivery (no message id)")
    return message_id


async def check(budget: Dict[str, Any], transport: Optional[Any] = None) -> bool:
    """Alert once per cycle when due; True when a message was sent."""
    if channel_reason() is not None or not should_alert(budget, load_alerted_cycle()):
        return False
    message_id = await send_telegram(format_message(budget), transport=transport)
    save_alerted_cycle(budget["cycle_start"])
    _log.info("✅ pace alert sent (message %d) for the cycle starting %s (projected %s%%)",
              message_id, budget["cycle_start"], budget["projected_pct"])
    return True
