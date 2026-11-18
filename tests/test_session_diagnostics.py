"""Session diagnostics over synthetic records: each flag, its thresholds and its heuristic wording."""

from __future__ import annotations

import pytest

from src import aggregate, session_diagnostics


def _types(diag: dict) -> list:
    return [f["type"] for f in diag["flags"]]


def _details(user_pct: int) -> list:
    return [
        {"category": "System", "label": "System Instructions", "percentageOfPrompt": 100 - user_pct},
        {"category": "User Context", "label": "Messages", "percentageOfPrompt": user_pct},
    ]


def test_large_prompt_flags_at_the_floor_when_the_median_is_small(rec):
    at = [rec(0, prompt_tokens=1_000), rec(1, prompt_tokens=20_000)]            # median 10.5k -> cut 20k
    just_under = [rec(0, prompt_tokens=1_000), rec(1, prompt_tokens=19_999)]
    diag = session_diagnostics.diagnose(at)
    assert _types(diag) == ["large_prompt"]
    assert _types(session_diagnostics.diagnose(just_under)) == []
    assert "heuristic" in diag["flags"][0]["text"]
    assert diag["heuristic"] is True


def test_large_prompt_flags_at_one_and_three_quarters_of_the_median_above_the_floor(rec):
    # Median 40k -> the cut is 70k, not the 20k floor.
    base = [rec(0, prompt_tokens=40_000), rec(1, prompt_tokens=40_000)]
    assert _types(session_diagnostics.diagnose(base + [rec(2, prompt_tokens=70_000)])) == ["large_prompt"]
    assert _types(session_diagnostics.diagnose(base + [rec(2, prompt_tokens=69_999)])) == []
    assert session_diagnostics.large_prompt_cut(
        [rec(i, prompt_tokens=n) for i, n in enumerate([40_000, 40_000, 70_000])]) == 70_000


def test_large_prompt_needs_two_observed_prompts(rec):
    assert _types(session_diagnostics.diagnose([rec(0, prompt_tokens=500_000)])) == []
    assert _types(session_diagnostics.diagnose([rec(0, prompt_tokens=500_000), rec(1, prompt_tokens=500_000)])) == []
    assert _types(session_diagnostics.diagnose([rec(0, prompt_tokens=1_000), rec(1, prompt_tokens=500_000)])) == [
        "large_prompt"]


def test_a_growing_prompt_is_not_a_flag_of_its_own(rec):
    growing = [rec(i, prompt_tokens=10_000 * (i + 1)) for i in range(4)]            # 10k -> 40k, median 25k
    diag = session_diagnostics.diagnose(growing)
    assert _types(diag) == []                                                       # 40k < 1.75 x 25k
    assert diag["growth_ratio"] == 4.0


def test_prompt_growth_lists_n_sized_requests_with_n_minus_one_changes(rec):
    sizes = [10_000, 15_000, 12_000, 12_000]
    diag = session_diagnostics.diagnose([rec(i, prompt_tokens=n) for i, n in enumerate(sizes)])
    growth = diag["prompt_growth"]
    assert [g["prompt_tokens"] for g in growth] == sizes
    assert [g["change_pct"] for g in growth] == [None, 50.0, -20.0, 0.0]
    assert sum(1 for g in growth if g["change_pct"] is not None) == len(sizes) - 1


def test_prompt_growth_leaves_out_unsized_and_estimated_requests(rec):
    records = [
        rec(0, prompt_tokens=8_000),
        rec(1, prompt_tokens=None),
        rec(2, prompt_tokens=9_000, usage_estimated=True),
        rec(3, prompt_tokens=10_000),
    ]
    growth = session_diagnostics.diagnose(records)["prompt_growth"]
    assert [g["prompt_tokens"] for g in growth] == [8_000, 10_000]
    assert growth[1]["change_pct"] == 25.0
    assert session_diagnostics.diagnose([])["prompt_growth"] == []


def _population(rec, user_pcts, days_ago: int = 0) -> list:
    """One three-request session per entry of ``user_pcts``, each with that user-context share."""
    return [
        rec(i, session_id="s%02d" % n, days_ago=days_ago, prompt_details=_details(user))
        for n, user in enumerate(user_pcts) for i in range(3)
    ]


@pytest.mark.parametrize("user_pct, flagged", [(30, True), (31, False)])
def test_high_non_user_share_flags_at_seventy_percent(rec, user_pct, flagged):
    diag = session_diagnostics.diagnose([rec(i, prompt_details=_details(user_pct)) for i in range(3)])
    assert diag["non_user_pct"] == 100.0 - user_pct
    assert _types(diag) == (["high_non_user_share"] if flagged else [])
    if flagged:
        assert "heuristic" in diag["flags"][0]["text"]


def test_non_user_share_needs_three_requests_with_a_composition(rec):
    thin = session_diagnostics.diagnose([rec(i, prompt_details=_details(3)) for i in range(2)])
    assert thin["non_user_pct"] is None and thin["flags"] == []


def test_the_flag_does_not_depend_on_other_sessions(rec):
    records = _population(rec, [3] * 12)                            # every session at 97%: all flagged
    assert sorted(k[1] for k in session_diagnostics.flags_by_session(records)) == ["s%02d" % n for n in range(12)]
    alone = session_diagnostics.flags_by_session(_population(rec, [3]))
    assert [f["type"] for f in alone[("Code", "s00")]] == ["high_non_user_share"]


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
        rec(0, prompt_tokens=2_000),
        rec(1, prompt_tokens=60_000),
        rec(2, session_id="sess-b", session_title="Quiet", prompt_tokens=2_000),
        rec(3, session_id="sess-b", session_title="Quiet", prompt_tokens=2_500),
    ]
    rows = {r["session_id"]: r for r in aggregate.get_sessions(records, "all")}
    assert [f["type"] for f in rows["sess-a"]["flags"]] == ["large_prompt"]
    assert rows["sess-b"]["flags"] == []

    detail = aggregate.get_session_detail(records, "sess-a")
    assert detail["diagnostics"]["max_prompt_tokens"] == 60_000
    assert _types(detail["diagnostics"]) == ["large_prompt"]
