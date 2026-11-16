"""Session diagnostics over synthetic records: each flag, its thresholds and its heuristic wording."""

from __future__ import annotations

from src import aggregate, session_diagnostics


def _types(diag: dict) -> list:
    return [f["type"] for f in diag["flags"]]


def _details(user_pct: int) -> list:
    return [
        {"category": "System", "label": "System Instructions", "percentageOfPrompt": 100 - user_pct},
        {"category": "User Context", "label": "Messages", "percentageOfPrompt": user_pct},
    ]


def test_large_prompt_flagged_at_threshold_only(rec):
    big = session_diagnostics.diagnose([rec(0, prompt_tokens=session_diagnostics.LARGE_PROMPT_TOKENS)])
    small = session_diagnostics.diagnose([rec(0, prompt_tokens=session_diagnostics.LARGE_PROMPT_TOKENS - 1)])
    assert _types(big) == ["large_prompt"]
    assert _types(small) == []
    assert "heuristic" in big["flags"][0]["text"]
    assert big["heuristic"] is True


def test_growing_prompt_needs_enough_requests_ratio_and_rise(rec):
    growing = [rec(i, prompt_tokens=10_000 * (i + 1)) for i in range(4)]            # 10k -> 40k
    diag = session_diagnostics.diagnose(growing)
    assert _types(diag) == ["growing_prompt"]
    assert diag["growth_ratio"] == 4.0
    assert "heuristic" in diag["flags"][0]["text"]

    assert _types(session_diagnostics.diagnose(growing[:3])) == []                  # too few turns
    small_rise = [rec(i, prompt_tokens=4_000 + 1_500 * i) for i in range(5)]       # 4k -> 10k
    assert _types(session_diagnostics.diagnose(small_rise)) == []                   # doubles, rises < 20k
    flat = [rec(i, prompt_tokens=30_000) for i in range(5)]
    assert _types(session_diagnostics.diagnose(flat)) == []


def test_high_non_user_share_needs_composition_on_enough_requests(rec):
    heavy = [rec(i, prompt_details=_details(3)) for i in range(3)]
    diag = session_diagnostics.diagnose(heavy)
    assert _types(diag) == ["high_non_user_share"]
    assert diag["non_user_pct"] == 97.0

    balanced = [rec(i, prompt_details=_details(30)) for i in range(3)]
    assert _types(session_diagnostics.diagnose(balanced)) == []
    assert session_diagnostics.diagnose(balanced)["non_user_pct"] == 70.0

    thin = [rec(i, prompt_details=_details(3)) for i in range(2)]
    unknown = session_diagnostics.diagnose(thin)
    assert unknown["non_user_pct"] is None and unknown["flags"] == []


def test_requests_without_composition_never_flag_non_user_share(rec):
    diag = session_diagnostics.diagnose([rec(i) for i in range(5)])
    assert diag["non_user_pct"] is None
    assert diag["flags"] == []


def test_estimated_usage_is_left_out_of_the_size_checks(rec):
    allocated = [rec(i, prompt_tokens=90_000, usage_estimated=True) for i in range(5)]
    diag = session_diagnostics.diagnose(allocated)
    assert diag["max_prompt_tokens"] == 0
    assert diag["flags"] == []


def test_session_rows_and_detail_carry_the_flags(rec):
    records = [
        rec(0, prompt_tokens=60_000),
        rec(1, session_id="sess-b", session_title="Quiet", prompt_tokens=2_000),
    ]
    rows = {r["session_id"]: r for r in aggregate.get_sessions(records, "all")}
    assert [f["type"] for f in rows["sess-a"]["flags"]] == ["large_prompt"]
    assert rows["sess-b"]["flags"] == []

    detail = aggregate.get_session_detail(records, "sess-a")
    assert detail["diagnostics"]["max_prompt_tokens"] == 60_000
    assert _types(detail["diagnostics"]) == ["large_prompt"]
