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


def test_non_user_share_is_reported_but_not_flagged_without_a_population_cut(rec):
    heavy = [rec(i, prompt_details=_details(3)) for i in range(3)]
    diag = session_diagnostics.diagnose(heavy)
    assert diag["non_user_pct"] == 97.0
    assert diag["flags"] == []

    balanced = [rec(i, prompt_details=_details(30)) for i in range(3)]
    assert session_diagnostics.diagnose(balanced, 50.0)["non_user_pct"] == 70.0

    thin = [rec(i, prompt_details=_details(3)) for i in range(2)]
    unknown = session_diagnostics.diagnose(thin, 50.0)
    assert unknown["non_user_pct"] is None and unknown["flags"] == []


def test_high_non_user_share_flags_only_the_top_of_the_machines_own_distribution(rec):
    records = _population(rec, [10, 9, 8, 7, 6, 5, 4, 3, 2, 1])   # non-user 90% .. 99%
    cut = session_diagnostics.non_user_cut(records)
    assert cut == 98.0
    flagged = session_diagnostics.flags_by_session(records)
    assert sorted(k[1] for k in flagged) == ["s09"]
    assert [f["type"] for f in flagged[("Code", "s09")]] == ["high_non_user_share"]


def test_a_uniformly_heavy_population_flags_nothing(rec):
    records = _population(rec, [3] * 12)                            # every session at 97%
    assert session_diagnostics.non_user_cut(records) == 97.0
    assert session_diagnostics.flags_by_session(records) == {}


def test_the_top_of_a_light_population_is_not_high_overhead(rec):
    records = _population(rec, [60, 59, 58, 57, 56, 55, 54, 53, 52, 40])   # non-user 40% .. 60%
    assert session_diagnostics.flags_by_session(records) == {}


def test_too_few_sessions_give_no_cut(rec):
    records = _population(rec, [10, 9, 8, 7, 6, 5, 4, 3, 2])        # one short of the minimum
    assert session_diagnostics.non_user_cut(records) is None
    assert session_diagnostics.flags_by_session(records) == {}


def test_flag_share_is_bounded_by_the_top_share(rec):
    records = _population(rec, list(range(40, 0, -1)))              # 40 distinct shares
    flagged = session_diagnostics.flags_by_session(records)
    assert len(flagged) <= session_diagnostics.NON_USER_TOP_SHARE * 40


def test_the_cut_does_not_move_with_the_period(rec):
    old = _population(rec, [10, 9, 8, 7, 6, 5, 4, 3, 2, 1], days_ago=400)          # 90% .. 99%
    recent = [rec(i, session_id="new", prompt_details=_details(0)) for i in range(3)]   # 100%
    everything = old + recent
    # Alone, this week has one session and no cut; only the all-time spread can flag it.
    rows = {r["session_id"]: r for r in aggregate.get_sessions(everything, "week")}
    assert [f["type"] for f in rows["new"]["flags"]] == ["high_non_user_share"]
    detail = aggregate.get_session_detail(everything, "new")
    assert [f["type"] for f in detail["diagnostics"]["flags"]] == ["high_non_user_share"]


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
