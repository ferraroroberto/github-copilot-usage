"""Per-session prompt diagnostics: large prompts, prompt growth, and the non-user share.

Everything here is a heuristic over the session's own requests. A flag says "this looks
worth a look", never "this is wasteful": the wording and the ``heuristic`` field carry
that to the UI. Copilot CLI requests whose usage was allocated from session totals are
left out of the size checks, because an even spread says nothing about any one prompt.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from src import prompt_composition
from src.vscode_parser import RequestRecord

# Absolute because a context window is absolute: a prompt this big re-sends a long context
# every later turn and is well short of the common 128k windows. On a local sample of 46
# sessions the largest prompt per session had its median near 21k and its 95th percentile
# near 53k, so this cut flags the long-context tail (6.5% there), not the typical session.
LARGE_PROMPT_TOKENS = 50_000
# Growth needs enough turns to be a trend rather than one big attachment. The same local
# sample had no session with this many sized requests, so the check fired on none of them;
# the three growth cuts stay conservative until multi-turn data says otherwise.
MIN_GROWTH_REQUESTS = 4
GROWTH_RATIO = 2.0
# Below this absolute rise a doubling is just a small prompt getting a bit bigger.
GROWTH_MIN_RISE_TOKENS = 20_000
# VS Code files only the user's own text and attachments as "User Context", so system and
# tool overhead is the bulk of nearly every prompt and its level depends on each machine's
# tools and instructions. A fixed cut flags most sessions on one machine and none on
# another, so the flag is relative: a session is flagged when its share is above the
# NON_USER_TOP_SHARE quantile of every session on this machine.
NON_USER_TOP_SHARE = 0.10
# Below this many sessions with a composition a quantile is noise, so the share is shown
# but never flagged.
MIN_SESSIONS_FOR_CUT = 10
# The relative cut only means "high overhead" when the session really is mostly overhead.
HIGH_NON_USER_FLOOR_PCT = 90.0
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


def non_user_cut(records: List[RequestRecord]) -> Optional[float]:
    """The non-user share a session must exceed to be flagged, or None with too few sessions.

    Nearest-rank quantile over every session that has a composition, so "strictly above"
    flags at most ``NON_USER_TOP_SHARE`` of them, ties included.
    """
    shares = sorted(
        pct for pct in (_non_user_pct(g) for g in _by_session(records).values()) if pct is not None
    )
    if len(shares) < MIN_SESSIONS_FOR_CUT:
        return None
    rank = math.ceil((1.0 - NON_USER_TOP_SHARE) * len(shares))
    return shares[rank - 1]


def diagnose(session_records: List[RequestRecord], high_non_user_cut: Optional[float] = None) -> Dict[str, Any]:
    """Diagnostics and flags for the requests of one session.

    ``high_non_user_cut`` is ``non_user_cut`` over all sessions; without it the non-user
    share is reported but never flagged.
    """
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
    if (
        non_user is not None and high_non_user_cut is not None
        and non_user > high_non_user_cut and non_user >= HIGH_NON_USER_FLOOR_PCT
    ):
        flags.append({
            "type": "high_non_user_share",
            "text": "About %d%% of the prompt is system and tool overhead rather than your own "
                    "context, among the highest of your sessions (heuristic)." % round(non_user),
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


def flags_by_session(
    records: List[RequestRecord], population: Optional[List[RequestRecord]] = None,
) -> Dict[SessionKey, List[Dict[str, str]]]:
    """``(ide, session_id)`` to its flags; sessions with none are left out.

    ``population`` is every record, so the relative cut does not move with the period.
    """
    out: Dict[SessionKey, List[Dict[str, str]]] = {}
    cut = non_user_cut(population if population is not None else records)
    for key, group in _by_session(records).items():
        flags = diagnose(group, cut)["flags"]
        if flags:
            out[key] = flags
    return out


def attach_flags(
    rows: List[dict], records: List[RequestRecord], population: Optional[List[RequestRecord]] = None,
) -> List[dict]:
    """Add ``flags`` to each session row, from the same records the rows were built from."""
    flags = flags_by_session(records, population)
    for row in rows:
        row["flags"] = flags.get((row["ide"], row["session_id"]), [])
    return rows
