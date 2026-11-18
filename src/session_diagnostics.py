"""Per-session prompt diagnostics: large prompts, prompt growth, and the non-user share.

Everything here is a heuristic over the session's own requests. A flag says "this looks
worth a look", never "this is wasteful": the wording and the ``heuristic`` field carry
that to the UI. Copilot CLI requests whose usage was allocated from session totals are
left out of the size checks, because an even spread says nothing about any one prompt.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from src import prompt_composition
from src.vscode_parser import RequestRecord

# A prompt is "large" at the larger of an absolute floor (a small prompt is never large
# however much it grew) and a multiple of the session's own median prompt, so a session that
# is always big flags only its outliers. One observation has no median to compare with.
LARGE_PROMPT_FLOOR_TOKENS = 20_000
LARGE_PROMPT_MEDIAN_RATIO = 1.75
MIN_PROMPT_OBSERVATIONS = 2
# VS Code files only the user's own text and attachments as "User Context", so a session is
# flagged when this share or more of its logged prompt is outside that.
HIGH_NON_USER_PCT = 70.0
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


def large_prompt_cut(sized: List[RequestRecord]) -> Optional[float]:
    """Tokens a prompt must reach to be flagged, or None with too few observed prompts."""
    if len(sized) < MIN_PROMPT_OBSERVATIONS:
        return None
    return max(LARGE_PROMPT_FLOOR_TOKENS, LARGE_PROMPT_MEDIAN_RATIO * statistics.median(r.prompt_tokens for r in sized))


def _prompt_growth(sized: List[RequestRecord]) -> List[Dict[str, Any]]:
    """Each sized prompt in order with its percentage change from the previous one.

    The first entry has no previous prompt, so its ``change_pct`` is None; N sized requests
    give N entries and N-1 changes.
    """
    out: List[Dict[str, Any]] = []
    previous: Optional[int] = None
    for r in sized:
        change = None if not previous else round(100.0 * (r.prompt_tokens - previous) / previous, 1)
        out.append({"request_id": r.request_id, "ts": r.ts.isoformat(), "prompt_tokens": r.prompt_tokens,
                    "change_pct": change})
        previous = r.prompt_tokens
    return out


def diagnose(session_records: List[RequestRecord]) -> Dict[str, Any]:
    """Diagnostics and flags for the requests of one session."""
    ordered = sorted(session_records, key=lambda r: (r.ts, r.request_id))
    sized = [r for r in ordered if not r.usage_estimated and (r.prompt_tokens or 0) > 0]
    flags: List[Dict[str, str]] = []

    max_prompt = max((r.prompt_tokens for r in sized), default=0)
    cut = large_prompt_cut(sized)
    if cut is not None and max_prompt >= cut:
        flags.append({
            "type": "large_prompt",
            "text": "Largest prompt was about %s tokens, above the %s cut: the larger of %s and %s "
                    "times the session's median prompt (heuristic)."
                    % (_k(max_prompt), _k(cut), _k(LARGE_PROMPT_FLOOR_TOKENS), LARGE_PROMPT_MEDIAN_RATIO),
        })

    first = sized[0].prompt_tokens if sized else 0
    last = sized[-1].prompt_tokens if sized else 0
    growth = round(last / first, 2) if len(sized) >= 2 and first else None

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
        "prompt_growth": _prompt_growth(sized),
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
