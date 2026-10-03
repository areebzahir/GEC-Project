"""Stage 5: contextual evidence for a (column, documented variable) pair.

Names stay the main signal; these small nudges use instance- and structure-level evidence (Rahm and
Bernstein's combined matchers, Ref [12]) to separate candidates whose names score about the same,
and to explain a match in plain words. Every nudge is returned with an evidence string, so a
reviewer can see exactly why a score moved (DESIGN.md section 14).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..readme.variable_layouts import VariableDefinition
from ..schema.document import ColumnType
from .normalize import normalize_name, tokenize
from .scorers import is_abbreviation

# Individual nudges. Small on purpose: context can break a tie, it cannot invent a match.
POSITION_BONUS = 0.03            # same ordinal position in the README list (+/- 1)
DESCRIPTION_FULL_BONUS = 0.04    # every column-name token is explained by the description
DESCRIPTION_PARTIAL_BONUS = 0.02  # at least half of them are
UNIT_AGREE_BONUS = 0.03          # unit cue in the name / units row agrees with the documented unit
UNIT_CONFLICT_PENALTY = -0.04    # ... or clearly names a different unit
UNIT_NUMERIC_BONUS = 0.02        # a documented unit implies a numeric column
UNIT_TEXT_PENALTY = -0.03        # documented unit, but the column holds free text
LABELS_SUBSET_BONUS = 0.03       # documented value-label codes all appear in the data
LABELS_DISJOINT_PENALTY = -0.05  # none of them appear
LABELS_CONTINUOUS_PENALTY = -0.05  # value labels documented, but the column is a continuous measure
# Caps on the summed nudges, so many weak cues never outweigh the name evidence.
MAX_CONTEXT_BONUS = 0.10
MAX_CONTEXT_PENALTY = -0.10
# Tokens shorter than this carry too little meaning to compare with a description.
_MIN_TOKEN_LEN = 2

# Spellings of the same unit, folded to one canonical part so "pct" agrees with "%".
_UNIT_ALIASES = {
    "pct": "%", "percent": "%", "percentage": "%", "perc": "%",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "centimeter": "cm", "centimeters": "cm", "millimeter": "mm", "millimeters": "mm",
    "kilometer": "km", "kilometers": "km",
    "gram": "g", "grams": "g", "kilogram": "kg", "kilograms": "kg", "kgs": "kg",
    "milligram": "mg", "milligrams": "mg", "microgram": "ug", "µg": "ug",
    "liter": "l", "liters": "l", "litre": "l", "litres": "l", "milliliter": "ml", "millilitre": "ml",
    "celsius": "c", "degc": "c", "°c": "c", "fahrenheit": "f", "degf": "f", "°f": "f", "kelvin": "k",
    "hour": "h", "hours": "h", "hr": "h", "hrs": "h", "day": "d", "days": "d",
    "minute": "min", "minutes": "min", "mins": "min", "second": "s", "seconds": "s", "sec": "s",
    "year": "yr", "years": "yr", "yrs": "yr",
}
# Canonical unit parts we recognise with confidence. A trailing name token is only read as a unit
# cue if it is one of these, and a conflict is only declared between two recognised units.
_UNIT_WORDS = frozenset({
    "%", "m", "cm", "mm", "km", "g", "kg", "mg", "ug", "l", "ml", "c", "f", "k",
    "h", "d", "min", "s", "yr", "ppm", "ppb", "mol", "mmol", "umol", "ntu",
})
_UNIT_SPLIT = re.compile(r"[^a-z0-9%µ°]+")
_BRACKET_UNIT = re.compile(r"[\(\[\{]([^\)\]\}]+)[\)\]\}]\s*$")
_WORD = re.compile(r"[a-z]+")


@dataclass(slots=True)
class ColumnContext:
    """What we know about a column, used for contextual scoring adjustments (stage 5)."""

    name: str
    position: int
    column_type: ColumnType
    distinct_values: frozenset[str]   # observed distinct raw values (small columns only)
    distinct_count: int
    header_unit: str | None = None    # unit from a units row under the header, if the reader found one


@dataclass(frozen=True, slots=True)
class Nudge:
    """One piece of contextual evidence and how much it moved the score."""

    delta: float
    evidence: str


def context_nudges(col: ColumnContext, defn: VariableDefinition, categorical_max: int) -> list[Nudge]:
    """All contextual evidence for one pair, in a fixed, readable order."""
    nudges: list[Nudge] = []
    if defn.position and abs((col.position + 1) - defn.position) <= 1:
        nudges.append(Nudge(POSITION_BONUS, f"same list position ({defn.position})"))
    nudges.extend(_description_nudge(col.name, defn.description))
    nudges.extend(_unit_nudges(col, defn.unit))
    nudges.extend(_value_label_nudges(col, defn, categorical_max))
    return nudges


def total_delta(nudges: list[Nudge]) -> float:
    """Sum the nudges, with bonuses and penalties capped separately."""
    bonus = min(MAX_CONTEXT_BONUS, sum(n.delta for n in nudges if n.delta > 0))
    penalty = max(MAX_CONTEXT_PENALTY, sum(n.delta for n in nudges if n.delta < 0))
    return bonus + penalty


# --------------------------------------------------------------------------- description tokens
def _description_nudge(column: str, description: str | None) -> list[Nudge]:
    """Column-name tokens explained by the description (``water_temp`` vs "water temperature")."""
    if not description:
        return []
    tokens = [t for t in tokenize(column)
              if len(t) >= _MIN_TOKEN_LEN and not t.isdigit() and _canonical_unit(t) not in _UNIT_WORDS]
    if not tokens:
        return []
    words = _WORD.findall(_fold(description))
    explained = [t for t in tokens if _token_in_words(t, words)]
    if len(explained) == len(tokens):
        return [Nudge(DESCRIPTION_FULL_BONUS, f"description explains the name ({', '.join(explained)})")]
    if len(explained) * 2 >= len(tokens):
        return [Nudge(DESCRIPTION_PARTIAL_BONUS, f"description explains part of the name ({', '.join(explained)})")]
    return []


def _token_in_words(token: str, words: list[str]) -> bool:
    """A name token matches a description word, abbreviates one, or is an acronym of a word run."""
    if any(token == w or is_abbreviation(token, w) for w in words):
        return True
    initials = "".join(w[0] for w in words)
    return len(token) >= _MIN_TOKEN_LEN and token.isalpha() and token in initials


# --------------------------------------------------------------------------- units
def _unit_nudges(col: ColumnContext, unit: str | None) -> list[Nudge]:
    nudges: list[Nudge] = []
    for label, cue in (("column name", _name_unit_cue(col.name)), ("units row", col.header_unit)):
        if not unit or not cue:
            continue
        verdict = compare_units(cue, unit)
        if verdict == "agree":
            nudges.append(Nudge(UNIT_AGREE_BONUS, f"unit in {label} ({cue}) agrees with documented unit ({unit})"))
        elif verdict == "conflict":
            nudges.append(Nudge(UNIT_CONFLICT_PENALTY, f"unit in {label} ({cue}) differs from documented unit ({unit})"))
    if unit and col.column_type in (ColumnType.NUMBER, ColumnType.INTEGER):
        nudges.append(Nudge(UNIT_NUMERIC_BONUS, "documented unit fits a numeric column"))
    elif unit and col.column_type is ColumnType.STRING and col.distinct_count > 0:
        nudges.append(Nudge(UNIT_TEXT_PENALTY, "documented with a unit, but the column holds text"))
    return nudges


def compare_units(a: str, b: str) -> str:
    """Return "agree", "conflict" or "unknown" for two unit strings (e.g. "pct" vs "%")."""
    parts_a, parts_b = unit_parts(a), unit_parts(b)
    if not parts_a or not parts_b:
        return "unknown"
    # Lenient agreement: a shared part, or one part prefixing another ("m" vs "m2").
    if any(x == y or x.startswith(y) or y.startswith(x) for x in parts_a for y in parts_b):
        return "agree"
    if (set(parts_a) & _UNIT_WORDS) and (set(parts_b) & _UNIT_WORDS):
        return "conflict"
    return "unknown"


def unit_parts(text: str) -> list[str]:
    """Split a unit into canonical lowercase parts: "mg/L" -> ["mg", "l"], "pct" -> ["%"]."""
    return [_canonical_unit(p) for p in _UNIT_SPLIT.split(_fold(text)) if p]


def _name_unit_cue(column: str) -> str | None:
    """A unit written into a column name: a bracketed suffix "(mg/L)" or a trailing "_kg"/"_pct"."""
    bracket = _BRACKET_UNIT.search(column)
    if bracket:
        return bracket.group(1).strip()
    if column.strip().endswith("%"):
        return "%"
    tokens = tokenize(column)
    if len(tokens) >= 2 and _canonical_unit(tokens[-1]) in _UNIT_WORDS:
        return tokens[-1]
    return None


def _canonical_unit(part: str) -> str:
    return _UNIT_ALIASES.get(part, part)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# --------------------------------------------------------------------------- value labels / type
def _value_label_nudges(col: ColumnContext, defn: VariableDefinition, categorical_max: int) -> list[Nudge]:
    """Documented codes vs observed values; labels on a continuous measure are a contradiction."""
    if not defn.value_labels:
        return []
    codes = {normalize_name(v.code) for v in defn.value_labels}
    observed = {normalize_name(v) for v in col.distinct_values}
    if codes and observed and codes <= observed:
        return [Nudge(LABELS_SUBSET_BONUS, "documented value codes all appear in the data")]
    numeric = col.column_type in (ColumnType.NUMBER, ColumnType.INTEGER)
    if numeric and col.distinct_count > categorical_max:
        return [Nudge(LABELS_CONTINUOUS_PENALTY, "value labels documented, but the column is a continuous measure")]
    if codes and observed and codes.isdisjoint(observed):
        return [Nudge(LABELS_DISJOINT_PENALTY, "none of the documented value codes appear in the data")]
    return []
