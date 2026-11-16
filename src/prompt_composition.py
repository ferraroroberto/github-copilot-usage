"""The per-request prompt composition, read one way for every consumer.

VS Code records, per request, a list of ``{category, label, percentageOfPrompt}`` entries.
A request's share of a label is that percentage of its ``prompt_tokens``: an estimate
from the logged percentage, never a measured per-tool count.
"""

from __future__ import annotations

from typing import List, Tuple

from src.vscode_parser import RequestRecord

# The category VS Code files the user's own text and attachments under; everything else
# (system instructions, tool definitions) is overhead the user does not type.
USER_CATEGORY = "User Context"


def label_tokens(r: RequestRecord) -> List[Tuple[str, str, float]]:
    """The request's ``(label, category, estimated tokens)`` entries; empty without composition."""
    if not r.prompt_details or r.prompt_tokens <= 0:
        return []
    out: List[Tuple[str, str, float]] = []
    for d in r.prompt_details:
        if not isinstance(d, dict):
            continue
        try:
            pct = float(d.get("percentageOfPrompt"))
        except (TypeError, ValueError):
            continue
        category = str(d.get("category") or "")
        label = str(d.get("label") or d.get("category") or "Other")
        out.append((label, category, r.prompt_tokens * pct / 100.0))
    return out


def has_composition(r: RequestRecord) -> bool:
    return bool(label_tokens(r))
