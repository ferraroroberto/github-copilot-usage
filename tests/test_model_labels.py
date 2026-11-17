"""Model label grouping, exact aliases, provider families and the model settings, on synthetic records."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from src import aggregate, config, context_drilldown, model_labels, request_explorer
from src.model_labels import ModelLabels

# Spellings of one model that differ in presentation only.
SAME_MODEL = [
    ["GPT-5 mini", "gpt-5-mini", "gpt_5_mini", "  GPT 5   Mini "],
    ["claude-sonnet-4-20250514", "claude-sonnet-4-2025-05-14", "Claude Sonnet 4", "claude-sonnet-4"],
    ["gpt-4o-2024-08-06", "GPT-4o"],
]

# Different models that must never share a row.
DISTINCT_MODELS = [
    "gpt-5", "gpt-5.1", "gpt-5-mini", "gpt-5-codex", "gpt-4", "gpt-4-0613", "gpt-4.1",
    "claude-sonnet-4", "claude-sonnet-4.5", "claude-sonnet-4-5", "claude-opus-4", "claude-sonnet-4-thinking",
    "o3", "o3-mini", "gemini-2.5-pro", "gemini-2.5-flash",
]


def _with_aliases(monkeypatch: pytest.MonkeyPatch, aliases: dict, colors: dict = None) -> None:
    monkeypatch.setattr(
        aggregate.config, "load",
        lambda: {"cycle_reset_day": 1, "monthly_credits": 300, "model_aliases": aliases, "model_colors": colors or {}},
    )


@pytest.mark.parametrize("spellings", SAME_MODEL)
def test_presentation_only_differences_share_one_key(spellings):
    assert len({model_labels.normalize(s) for s in spellings}) == 1


def test_distinct_models_keep_distinct_keys():
    keys = [model_labels.normalize(name) for name in DISTINCT_MODELS]
    assert len(set(keys)) == len(DISTINCT_MODELS)


def test_blank_and_missing_models_read_as_unknown():
    assert model_labels.normalize("") == "unknown"
    assert model_labels.normalize(None) == "unknown"
    assert model_labels.normalize("  - ") == "unknown"


@pytest.mark.parametrize("name,family", [
    ("GPT-5 mini", "openai"), ("o3-mini", "openai"), ("codex-max", "openai"),
    ("Claude Sonnet 4.5", "anthropic"), ("haiku-3", "anthropic"),
    ("Gemini 2.5 Pro", "google"), ("grok-code-fast-1", "xai"),
    ("Phi-4", "microsoft"), ("MAI-Code-1.1-Flash", "microsoft"), ("Raptor mini", "other"), ("sapphire", "other"), ("unknown", "other"),
])
def test_family_follows_the_provider(name, family):
    assert model_labels.family_of(model_labels.normalize(name)) == family


def test_the_most_frequent_spelling_labels_the_group_and_ties_go_alphabetical():
    labels = ModelLabels({"GPT-5 mini": 3, "gpt-5-mini": 1, "claude-sonnet-4": 2, "Claude Sonnet 4": 2}, {})
    assert labels.label("gpt-5-mini") == "GPT-5 mini"
    assert labels.label("Claude Sonnet 4") == "Claude Sonnet 4"
    assert labels.label("claude-sonnet-4") == "Claude Sonnet 4"
    rows = {row["key"]: row for row in labels.catalog()}
    assert rows["gpt-5-mini"]["raw"] == ["GPT-5 mini", "gpt-5-mini"]


def test_alias_merges_into_the_target_and_clearing_it_restores_both():
    counts = {"gpt-5-mini": 5, "GPT-5 Mini Preview": 2, "gpt-5": 1}
    merged = ModelLabels(counts, {"gpt-5-mini-preview": "gpt-5-mini"})
    assert merged.label("GPT-5 Mini Preview") == "gpt-5-mini"
    assert merged.key("GPT-5 Mini Preview") == merged.key("gpt-5-mini") == "gpt-5-mini"
    assert sorted(g.label for g in merged.groups()) == ["gpt-5", "gpt-5-mini"]
    assert merged.groups()[0].raw == ("gpt-5-mini", "GPT-5 Mini Preview")

    cleared = ModelLabels(counts, {})
    assert cleared.label("GPT-5 Mini Preview") == "GPT-5 Mini Preview"
    assert len(cleared.groups()) == 3


def test_catalog_lists_models_as_spelled_before_any_alias():
    labels = ModelLabels({"a-model": 3, "b-model": 2}, {"b-model": "a-model"})
    rows = {row["key"]: row for row in labels.catalog()}
    assert set(rows) == {"a-model", "b-model"}
    assert rows["b-model"]["alias"] == "a-model" and rows["a-model"]["alias"] is None


def test_alias_to_a_model_without_history_is_labelled_by_its_members():
    labels = ModelLabels({"Old Name": 2}, {"old-name": "new-name"})
    assert [g.key for g in labels.groups()] == ["new-name"]
    assert labels.label("Old Name") == "Old Name"


def test_validate_aliases_normalises_and_rejects_loops_and_chains():
    assert model_labels.validate_aliases({"GPT 5 Mini": "gpt-5"}) == {"gpt-5-mini": "gpt-5"}
    for bad in (
        [], {"a": 1}, {"": "b"}, {"a": " "},
        {"a": "A"},                               # merges into itself once normalised
        {"a": "b", "b": "c"},                     # a chain
        {"a": "b", "b": "a"},                     # a loop
        {"Gpt-5": "x", "gpt_5": "y"},             # one name twice
    ):
        with pytest.raises(ValueError):
            model_labels.validate_aliases(bad)


def test_aliases_are_exact_names_never_patterns():
    labels = ModelLabels({"gpt-5-mini": 1, "gpt-5": 1, "gpt-5.1": 1}, {"gpt-5*": "gpt-5"})
    assert len(labels.groups()) == 3


def test_validate_colors_accepts_family_and_opacity():
    assert model_labels.validate_colors({"GPT 5": {"family": "google", "opacity": 0.6}, "x": {}}) == {
        "gpt-5": {"family": "google", "opacity": 0.6},
    }


@pytest.mark.parametrize("bad", [
    [], {"m": "red"}, {"m": {"color": "#fff"}}, {"m": {"family": "nvidia"}}, {"m": {"family": 3}},
    {"m": {"opacity": 0.1}}, {"m": {"opacity": 1.5}}, {"m": {"opacity": True}}, {"m": {"opacity": "0.5"}},
    {"m": {"opacity": float("nan")}}, {"": {"opacity": 1}}, {"M": {"opacity": 1}, "m": {"opacity": 0.5}},
])
def test_validate_colors_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        model_labels.validate_colors(bad)


# --- config: save and load ---------------------------------------------------------------------


def test_save_rejects_unknown_and_read_only_keys_and_writes_nothing(settings_file):
    with pytest.raises(config.ConfigError, match="unknown setting"):
        config.save({"monthly_credits": 500, "bogus": 1})
    with pytest.raises(config.ConfigError, match="not editable"):
        config.save({"port": 1234})
    assert not settings_file.exists()
    assert config.load()["monthly_credits"] == config.DEFAULTS["monthly_credits"]


def test_save_rejects_bad_colour_values(settings_file):
    for bad in ({"m": {"family": "nvidia"}}, {"m": {"opacity": 7}}, {"m": {"hue": 1}}):
        with pytest.raises(config.ConfigError, match="model_colors"):
            config.save({"model_colors": bad})
    assert not settings_file.exists()


def test_save_rejects_a_malformed_number_instead_of_crashing(settings_file):
    with pytest.raises(config.ConfigError, match="monthly_credits"):
        config.save({"monthly_credits": "lots"})


def test_save_persists_the_model_settings(settings_file):
    saved = config.save({
        "model_aliases": {"GPT 5 Mini Preview": "gpt-5-mini"},
        "model_colors": {"gpt-5-mini": {"family": "anthropic", "opacity": 0.6}},
    })
    assert saved["model_aliases"] == {"gpt-5-mini-preview": "gpt-5-mini"}
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["model_colors"] == {"gpt-5-mini": {"family": "anthropic", "opacity": 0.6}}
    assert config.save({"model_aliases": {}})["model_aliases"] == {}
    assert json.loads(settings_file.read_text(encoding="utf-8"))["model_aliases"] == {}


def test_load_ignores_a_bad_model_setting_in_the_file(settings_file):
    settings_file.write_text(json.dumps({
        "monthly_credits": 42, "model_aliases": {"a": "a"}, "model_colors": {"m": {"opacity": 0.5}}, "mystery": 1,
    }), encoding="utf-8")
    cfg = config.load()
    assert cfg["monthly_credits"] == 42
    assert cfg["model_aliases"] == {}
    assert cfg["model_colors"] == {"m": {"opacity": 0.5}}
    assert "mystery" not in cfg


def test_api_config_refuses_a_bad_update_with_its_reason(settings_file):
    from app.server import app

    client = TestClient(app)
    resp = client.post("/api/config", json={"model_colors": {"m": {"family": "nvidia"}}})
    assert resp.status_code == 400
    assert "family" in resp.json()["detail"]
    assert client.post("/api/config", json={"nope": 1}).status_code == 400
    assert client.post("/api/config", json={"model_aliases": {"a": "b"}}).status_code == 200


# --- the views that read the groups ------------------------------------------------------------


@pytest.fixture
def spelled(rec):
    return [
        rec(0, model="GPT-5 mini", credits=2.0),
        rec(1, model="gpt-5-mini", credits=1.0),
        rec(2, model="gpt_5_mini", credits=1.0, session_id="sess-b", session_title="Beta"),
        rec(3, model="gpt-5", credits=4.0),
        rec(4, model="gpt-5.1", credits=3.0),
    ]


def test_summary_groups_presentation_variants_and_keeps_distinct_versions(spelled):
    body = aggregate.get_summary(spelled, "today")
    rows = {row["model"]: row for row in body["by_model"]}
    assert set(rows) == {"GPT-5 mini", "gpt-5", "gpt-5.1"}
    mini = rows["GPT-5 mini"]
    assert (mini["requests"], mini["credits"]) == (3, 4.0)
    assert (mini["model_key"], mini["family"]) == ("gpt-5-mini", "openai")
    assert mini["raw_models"] == ["GPT-5 mini", "gpt-5-mini", "gpt_5_mini"]
    assert body["model_meta"]["GPT-5 mini"] == {"key": "gpt-5-mini", "family": "openai"}
    today = body["time_series"][-1]["models"]
    assert set(today) == {"GPT-5 mini", "gpt-5", "gpt-5.1"}
    sessions = {s["session_id"]: s for s in body["top_sessions"]}
    assert sessions["sess-b"]["models"] == ["GPT-5 mini"]


def test_an_alias_merges_models_in_every_view_and_clearing_it_restores_them(spelled, monkeypatch):
    _with_aliases(monkeypatch, {"gpt-5.1": "gpt-5"})
    body = aggregate.get_summary(spelled, "today")
    assert {row["model"]: row["requests"] for row in body["by_model"]} == {"GPT-5 mini": 3, "gpt-5": 2}
    assert set(body["time_series"][-1]["models"]) == {"GPT-5 mini", "gpt-5"}

    _with_aliases(monkeypatch, {})
    restored = aggregate.get_summary(spelled, "today")
    assert {row["model"] for row in restored["by_model"]} == {"GPT-5 mini", "gpt-5", "gpt-5.1"}


def test_request_detail_keeps_the_raw_label(spelled):
    detail = aggregate.get_session_detail(spelled, "sess-b")
    assert detail["requests_detail"][0]["model"] == "gpt_5_mini"
    assert detail["requests_detail"][0]["model_label"] == "GPT-5 mini"
    assert detail["models"] == ["GPT-5 mini"]
    assert "gpt_5_mini" in aggregate.export_csv(spelled, "all")


def test_request_explorer_filters_and_counts_by_group(spelled, monkeypatch):
    body = request_explorer.explore(spelled, request_explorer.Query.build(period="week"))
    facet = {o["value"]: o for o in body["facets"]["model"]}
    assert facet["gpt-5-mini"]["count"] == 3 and facet["gpt-5-mini"]["label"] == "GPT-5 mini"
    assert facet["gpt-5"]["count"] == 1 and facet["gpt-5.1"]["count"] == 1

    for value in ("gpt-5-mini", "GPT-5 mini", "gpt_5_mini"):
        filtered = request_explorer.explore(spelled, request_explorer.Query.build(period="week", model=value))
        assert filtered["total"] == 3
        assert filtered["query"]["model"] == "gpt-5-mini"
        assert {row["model"] for row in filtered["rows"]} == {"GPT-5 mini", "gpt-5-mini", "gpt_5_mini"}
        assert {row["model_label"] for row in filtered["rows"]} == {"GPT-5 mini"}

    _with_aliases(monkeypatch, {"gpt-5.1": "gpt-5"})
    merged = request_explorer.explore(spelled, request_explorer.Query.build(period="week", model="gpt-5.1"))
    assert merged["total"] == 2 and merged["query"]["model"] == "gpt-5"


def test_context_drilldown_groups_models(spelled):
    for r in spelled:
        r.prompt_details = [{"category": "System", "label": "Tool Definitions", "percentageOfPrompt": 100}]
    body = context_drilldown.drilldown(spelled, "all", "")
    assert {row["model"]: row["requests"] for row in body["models"]} == {"GPT-5 mini": 3, "gpt-5": 1, "gpt-5.1": 1}


def test_api_models_lists_the_history_for_the_settings_choices(spelled, monkeypatch):
    from app.server import app

    monkeypatch.setattr(aggregate, "gather_records", lambda: (spelled, []))
    body = TestClient(app).get("/api/models").json()
    assert [f["id"] for f in body["families"]] == ["google", "anthropic", "xai", "openai", "microsoft", "other"]
    assert body["models"][0]["key"] == "gpt-5-mini" and body["models"][0]["requests"] == 3
