"""Feature blocks: the coarse parts of the app that can be switched off per machine.

A block is one coherent part that sits on one source or primitive (an integration, a data source, a
background poller), never a single button. Every block is on until Settings → Available features says
otherwise; the choice is stored in ``config.json`` (machine-local, never committed), so the code is the
same on every machine and only the switches differ.

A block can also be *unavailable* because what it needs is missing here (a token, a folder, a Python
version). That is reported with its reason and is separate from the switch: the switch stays usable so a
block that cannot run on this machine can be turned off and its UI disappears.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from src import billing, config, discovery, pace_alert

_log = logging.getLogger(__name__)


def _copilot_cli_reason() -> Optional[str]:
    if discovery.copilot_cli_dir() is None:
        return "no Copilot CLI session folder on this machine"
    return None


def _billing_reason() -> Optional[str]:
    if not billing.pat_configured():
        return "%s is not set in .env" % billing.PAT_ENV
    return None


def _usage_log_reason() -> Optional[str]:
    try:
        importlib.import_module("src.usage_log")
    except Exception as exc:  # the vendored log targets a newer Python than this app promises
        return "%s: %s" % (type(exc).__name__, exc)
    return None


@dataclass(frozen=True)
class Block:
    id: str
    label: str
    covers: str
    # Why the block cannot run on this machine, or None when it can.
    reason: Callable[[], Optional[str]]


BLOCKS = (
    Block(
        "copilot_cli", "Copilot CLI sessions",
        "Reads the terminal Copilot CLI's session logs into every total, list and chart.",
        _copilot_cli_reason,
    ),
    Block(
        "billing_card", "Official billing card",
        "The Official GitHub billing card, its Billing token row and its calls to api.github.com.",
        _billing_reason,
    ),
    Block(
        "pace_alert", "Pace alert",
        "The Telegram message sent when the cycle projection reaches the allowance, and the timer behind it.",
        pace_alert.channel_reason,
    ),
    Block(
        "usage_log", "Usage log",
        "The local record of which views and controls are used, in the page and in its SQLite file.",
        _usage_log_reason,
    ),
)

_BY_ID: Dict[str, Block] = {block.id: block for block in BLOCKS}


def describe() -> List[Dict[str, Any]]:
    """Every block with its switch and whether this machine can run it."""
    config.load()  # .env values must be in the environment before a block reads its token
    out: List[Dict[str, Any]] = []
    for block in BLOCKS:
        reason = block.reason()
        out.append({
            "id": block.id,
            "label": block.label,
            "covers": block.covers,
            "enabled": config.feature_enabled(block.id),
            "available": reason is None,
            "reason": reason,
        })
    return out


def set_enabled(block_id: str, enabled: bool) -> None:
    """Switch one block; raises KeyError for an unknown id. Only a block that is off is stored."""
    if block_id not in _BY_ID:
        raise KeyError(block_id)
    off = dict(config.load()["features"])
    if enabled:
        off.pop(block_id, None)
    else:
        off[block_id] = False
    config.save({"features": off})
    _log.info("ℹ️ feature %s: %s", block_id, "on" if enabled else "off")
