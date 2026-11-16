"""Per-session prompt diagnostics: large prompts, prompt growth, and the non-user share.

Everything here is a heuristic over the session's own requests. A flag says "this looks
worth a look", never "this is wasteful": the wording and the ``heuristic`` field carry
that to the UI. Copilot CLI requests whose usage was allocated from session totals are
left out of the size checks, because an even spread says nothing about any one prompt.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from src import prompt_composition
from src.vscode_parser import RequestRecord

# A prompt this big is a long context that every later turn re-sends; typical chat
# prompts sit well below it, and common context windows start around 128k.
LARGE_PROMPT_TOKENS = 50_000
# Growth needs enough turns to be a trend rather than one big attachment.
MIN_GROWTH_REQUESTS = 4
GROWTH_RATIO = 2.0
# Below this absolute rise a doubling is just a small prompt getting a bit bigger.
GROWTH_MIN_RISE_TOKENS = 20_000
# VS Code files only the user's own text and attachments as "User Context"; at 95% or
# more overhead the session is mostly resending instructions and tool definitions.
HIGH_NON_USER_PCT = 95.0
MIN_COVERED_REQUESTS = 3

SessionKey = Tuple[str, str]


def _k(tokens: float) -> str:
    return "%dk" % round(tokens / 1000.0) if tokens >= 1000 else str(int(tokens))


def _non_user_pct(records: List[RequestRecord]) -> Optional[float]:
    """Estimated share of the prompt outside the user's own context, or None when unknown."""
    covered = 0
    total = 0.0
    user = 0.0
    for r in records:
        entries = prompt_composition.label_tokens(r)
        if not entries:
            continue
        covered += 1
        for _label, category, tokens in entries:
            total += tokens
            if category == prompt_composition.USER_CATEGORY:
                user += tokens
    if covered < MIN_COVERED_REQUESTS or not total:
        return None
    return round(100.0 * (1.0 - user / total), 1)


def diagnose(session_records: List[RequestRecord]) -> Dict[str, Any]:
    """Diagnostics and flags for the requests of one session."""
    ordered = sorted(session_records, key=lambda r: (r.ts, r.request_id))
    sized = [r for r in ordered if not r.usage_estimated and r.prompt_tokens > 0]
    flags: List[Dict[str, str]] = []

    max_prompt = max((r.prompt_tokens for r in sized), default=0)
    if max_prompt >= LARGE_PROMPT_TOKENS:
        flags.append({
            "type": "large_prompt",
            "text": "Largest prompt was about %s tokens, above the %s rule of thumb (heuristic)."
                    % (_k(max_prompt), _k(LARGE_PROMPT_TOKENS)),
        })

    first = sized[0].prompt_tokens if sized else 0
    last = sized[-1].prompt_tokens if sized else 0
    growth = round(last / first, 2) if len(sized) >= 2 and first else None
    if (
        len(sized) >= MIN_GROWTH_REQUESTS and growth is not None
        and growth >= GROWTH_RATIO and last - first >= GROWTH_MIN_RISE_TOKENS
    ):
        flags.append({
            "type": "growing_prompt",
            "text": "Prompts grew from about %s to %s tokens over %d requests; a long session "
                    "re-sends its history, so a fresh one may be lighter (heuristic)."
                    % (_k(first), _k(last), len(sized)),
        })

    non_user = _non_user_pct(ordered)
    if non_user is not None and non_user >= HIGH_NON_USER_PCT:
        flags.append({
            "type": "high_non_user_share",
            "text": "About %d%% of the prompt is system and tool overhead rather than your own "
                    "context (heuristic)." % round(non_user),
        })

    return {
        "heuristic": True,
        "requests": len(ordered),
        "max_prompt_tokens": max_prompt,
        "first_prompt_tokens": first,
        "last_prompt_tokens": last,
        "growth_ratio": growth,
        "non_user_pct": non_user,
        "flags": flags,
    }


def _by_session(records: List[RequestRecord]) -> Dict[SessionKey, List[RequestRecord]]:
    groups: Dict[SessionKey, List[RequestRecord]] = defaultdict(list)
    for r in records:
        groups[(r.ide, r.session_id)].append(r)
    return groups


def flags_by_session(records: List[RequestRecord]) -> Dict[SessionKey, List[Dict[str, str]]]:
    """``(ide, session_id)`` to its flags; sessions with none are left out."""
    out: Dict[SessionKey, List[Dict[str, str]]] = {}
    for key, group in _by_session(records).items():
        flags = diagnose(group)["flags"]
        if flags:
            out[key] = flags
    return out


def attach_flags(rows: List[dict], records: List[RequestRecord]) -> List[dict]:
    """Add ``flags`` to each session row, from the same records the rows were built from."""
    flags = flags_by_session(records)
    for row in rows:
        row["flags"] = flags.get((row["ide"], row["session_id"]), [])
    return rows
