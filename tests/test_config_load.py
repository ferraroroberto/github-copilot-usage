"""A hand-edited config.json: every key is checked at load, a bad one falls back to its default with a log warning."""

from __future__ import annotations

import json
import logging
from datetime import date

import pytest

from src import aggregate, config


def _load_with(settings_file, raw):
    settings_file.write_text(json.dumps(raw), encoding="utf-8")
    return config.load()


def test_every_default_key_has_a_validator():
    # A key added to DEFAULTS without a cleaner would reach the code unchecked from a hand-edited file.
    assert set(config.DEFAULTS) == set(config._VALIDATORS)


@pytest.mark.parametrize("key, bad", [
    ("cycle_reset_day", "first"),
    ("cycle_reset_day", None),
    ("cycle_reset_day", True),
    ("monthly_credits", "1,500"),
    ("monthly_credits", [1]),
    ("monthly_credits", False),
    ("port", "eighty"),
    ("port", 0),
    ("port", 70000),
    ("port", -1),
    ("extra_roots", "C:/one"),
    ("extra_roots", {"a": 1}),
])
def test_bad_hand_edited_value_falls_back_to_default_and_warns(settings_file, caplog, key, bad):
    with caplog.at_level(logging.WARNING, logger="src.config"):
        cfg = _load_with(settings_file, {key: bad})
    assert cfg[key] == config.DEFAULTS[key]
    assert "ignoring %s" % key in caplog.text


def test_hand_edited_reset_day_and_credits_are_cleaned_like_a_saved_one(settings_file):
    cfg = _load_with(settings_file, {"cycle_reset_day": 31, "monthly_credits": "1500"})
    assert cfg["cycle_reset_day"] == 28
    assert cfg["monthly_credits"] == 1500
    cfg = _load_with_fresh(settings_file, {"cycle_reset_day": 0, "monthly_credits": -5})
    assert cfg["cycle_reset_day"] == 1
    assert cfg["monthly_credits"] == 0


def test_hand_edited_extra_roots_drop_blank_entries(settings_file):
    assert _load_with(settings_file, {"extra_roots": ["a", " ", ""]})["extra_roots"] == ["a"]


def test_a_valid_port_is_kept(settings_file):
    assert _load_with(settings_file, {"port": 9000})["port"] == 9000


def test_out_of_range_port_env_is_ignored(settings_file, monkeypatch, caplog):
    settings_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COPILOT_USAGE_PORT", "99999")
    with caplog.at_level(logging.WARNING, logger="src.config"):
        assert config.load()["port"] == config.DEFAULTS["port"]
    assert "COPILOT_USAGE_PORT" in caplog.text


def test_budget_math_survives_bad_hand_edited_values(settings_file):
    _load_with(settings_file, {"cycle_reset_day": 0, "monthly_credits": "1,500"})
    assert aggregate.cycle_start(date(2026, 3, 15)) == date(2026, 3, 1)
    assert aggregate.budget_status([], date(2026, 3, 15))["allowance_credits"] == config.DEFAULTS["monthly_credits"]


def _load_with_fresh(settings_file, raw):
    config._loaded = False
    return _load_with(settings_file, raw)
