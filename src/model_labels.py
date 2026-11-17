"""Model labels: one row per model, however Copilot spelled it.

Copilot writes the same model several ways ("GPT-5 mini", "gpt-5-mini", "gpt_5_mini",
"claude-sonnet-4-20250514" next to "claude-sonnet-4"). ``normalize`` removes only
presentation: case, spacing, the separators space / underscore / hyphen, and a trailing
release date. It never merges different families, versions or variants ("gpt-5" and
"gpt-5.1", or "gpt-5" and "gpt-5-mini", stay apart). Anything beyond that is the user's own
decision, written as an exact alias between two normalised names: no pattern, no fuzzy match.

The raw label is never lost: records keep it, and a group lists every raw label it holds.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Tuple

UNKNOWN = "unknown"

# (id, label), in the order the chart stacks them; each family has one brand colour in the front end.
FAMILIES: Tuple[Tuple[str, str], ...] = (
    ("google", "Google"),
    ("anthropic", "Anthropic"),
    ("xai", "xAI"),
    ("openai", "OpenAI"),
    ("microsoft", "Microsoft"),
    ("other", "Other providers"),
)
FAMILY_IDS = tuple(family for family, _ in FAMILIES)

# Below this a coloured series fades into the card; the Settings choices stay well above it.
MIN_OPACITY = 0.2

_SEPARATORS = re.compile(r"[\s_\-]+")
# A snapshot date after the name: -20250514, -2025-05-14. Four digits alone (gpt-4-0613) stay.
_DATE_SUFFIX = re.compile(r"-20\d{2}-?(?:0[1-9]|1[0-2])-?(?:0[1-9]|[12]\d|3[01])$")

_FAMILY_RULES = (
    ("anthropic", re.compile(r"claude|(^|-)(haiku|sonnet|opus)(-|$)")),
    ("google", re.compile(r"gemini|gemma")),
    ("xai", re.compile(r"grok")),
    ("openai", re.compile(r"(^|-)(gpt|chatgpt|codex)|^o\d+(-|$)")),
    ("microsoft", re.compile(r"^mai-|(^|-)phi-?\d|microsoft")),
)


def normalize(raw: Any) -> str:
    """The presentation-free name two spellings of one model share."""
    text = unicodedata.normalize("NFKC", str(raw or "")).casefold().strip()
    text = _SEPARATORS.sub("-", text).strip("-")
    undated = _DATE_SUFFIX.sub("", text).strip("-")
    return undated or text or UNKNOWN


def family_of(key: str) -> str:
    """The provider family a normalised name belongs to; ``other`` when no rule matches."""
    for family, pattern in _FAMILY_RULES:
        if pattern.search(key):
            return family
    return "other"


def family_options() -> List[Dict[str, str]]:
    return [{"id": family, "label": label} for family, label in FAMILIES]


def validate_aliases(value: Any) -> Dict[str, str]:
    """The alias map with every name normalised; raises ValueError on a malformed one.

    A name may merge into one other name, never into a name that is itself merged away,
    so resolving takes a single lookup and cannot loop.
    """
    if not isinstance(value, dict):
        raise ValueError("must be an object mapping a model name to the model it merges into")
    out: Dict[str, str] = {}
    for source, target in value.items():
        if not isinstance(source, str) or not isinstance(target, str) or not source.strip() or not target.strip():
            raise ValueError("every alias needs a non-empty model name on both sides")
        src, dst = normalize(source), normalize(target)
        if src == dst:
            raise ValueError("%r cannot merge into itself" % src)
        if src in out:
            raise ValueError("%r is listed twice" % src)
        out[src] = dst
    for src, dst in out.items():
        if dst in out:
            raise ValueError("%r merges into %r, which is itself merged into %r; point it at %r" % (src, dst, out[dst], out[dst]))
    return out


def validate_colors(value: Any) -> Dict[str, Dict[str, Any]]:
    """The per-model colour overrides with names normalised; raises ValueError on a bad one."""
    if not isinstance(value, dict):
        raise ValueError("must be an object mapping a model name to its colour override")
    out: Dict[str, Dict[str, Any]] = {}
    for name, entry in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("every colour override needs a non-empty model name")
        key = normalize(name)
        if key in out:
            raise ValueError("%r is listed twice" % key)
        if not isinstance(entry, dict):
            raise ValueError("%r: the override must be an object with family and/or opacity" % key)
        unknown = sorted(set(entry) - {"family", "opacity"})
        if unknown:
            raise ValueError("%r: unknown key(s) %s (allowed: family, opacity)" % (key, ", ".join(unknown)))
        clean: Dict[str, Any] = {}
        if "family" in entry:
            if entry["family"] not in FAMILY_IDS:
                raise ValueError("%r: family must be one of %s" % (key, ", ".join(FAMILY_IDS)))
            clean["family"] = entry["family"]
        if "opacity" in entry:
            opacity = entry["opacity"]
            if isinstance(opacity, bool) or not isinstance(opacity, (int, float)) or not math.isfinite(opacity):
                raise ValueError("%r: opacity must be a number" % key)
            if not MIN_OPACITY <= opacity <= 1:
                raise ValueError("%r: opacity must be between %s and 1" % (key, MIN_OPACITY))
            clean["opacity"] = round(float(opacity), 2)
        if clean:
            out[key] = clean
    return out


@dataclass(frozen=True)
class ModelGroup:
    """Every raw label that reads as one model."""

    key: str                  # normalised name; what aliases, colour overrides and filters use
    label: str                # the raw spelling shown for the group: its most frequent one
    family: str
    raw: Tuple[str, ...]      # every raw label in the group, most frequent first
    requests: int


def _pick_label(counts: Mapping[str, int]) -> str:
    """The most frequent spelling; a tie goes to the alphabetically first so the label is stable."""
    return min(counts.items(), key=lambda item: (-item[1], item[0]))[0]


def _group(key: str, counts: Mapping[str, int], label_counts: Mapping[str, int]) -> ModelGroup:
    raw = tuple(name for name, _n in sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    return ModelGroup(key, _pick_label(label_counts), family_of(key), raw, sum(counts.values()))


class ModelLabels:
    """The grouping of a set of raw model labels, with the user's aliases applied."""

    def __init__(self, counts: Mapping[str, int], aliases: Mapping[str, str]) -> None:
        self._aliases = dict(aliases)
        natural: Dict[str, Counter] = {}
        for raw, n in counts.items():
            natural.setdefault(normalize(raw), Counter())[raw or UNKNOWN] += n
        self._natural = {key: _group(key, c, c) for key, c in natural.items()}

        merged: Dict[str, Counter] = {}
        for key, c in natural.items():
            merged.setdefault(self._aliases.get(key, key), Counter()).update(c)
        # A merged group carries its target's own spelling when the target has history.
        self._groups = {
            key: _group(key, c, natural.get(key, c)) for key, c in merged.items()
        }
        self._by_raw = {
            raw: key for key, g in self._groups.items() for raw in g.raw
        }

    def key(self, raw: Any) -> str:
        """The group key of a raw label, or of a label or key the UI sends back."""
        raw = raw or UNKNOWN
        known = self._by_raw.get(raw)
        if known is not None:
            return known
        key = normalize(raw)
        return self._aliases.get(key, key)

    def label(self, raw: Any) -> str:
        group = self._groups.get(self.key(raw))
        return group.label if group else (raw or UNKNOWN)

    def groups(self) -> List[ModelGroup]:
        return list(self._groups.values())

    def meta(self) -> Dict[str, Dict[str, str]]:
        """Label -> key and family: what the front end needs to colour a series."""
        return {g.label: {"key": g.key, "family": g.family} for g in self._groups.values()}

    def catalog(self) -> List[Dict[str, Any]]:
        """Every model as spelled before any alias, for the Settings choices."""
        rows = [
            {
                "key": g.key, "label": g.label, "family": g.family, "raw": list(g.raw),
                "requests": g.requests, "alias": self._aliases.get(g.key),
            }
            for g in self._natural.values()
        ]
        rows.sort(key=lambda row: (-row["requests"], row["label"].lower()))
        return rows
