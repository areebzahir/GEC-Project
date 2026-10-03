"""Per-stage similarity scorers for matching a column name to a documented variable name.

Each stage is a small pure function returning ``(score in [0,1], method, evidence)`` (DESIGN.md
section 14). Stages run most-specific first; the first stage that fires wins the base score. The
lexical fuzzy stage uses RapidFuzz Indel ``ratio`` — chosen over Jaro-Winkler because its prefix
bonus produced false positives in our research (``milk_prt`` vs ``milk_fat`` scored 88.6) (Ref [5][24]).
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from .normalize import normalize_name, tokenize

# Base scores per stage, so the method is reflected in the confidence (DESIGN.md section 14 table).
_SCORE_EXACT = 1.00
_SCORE_CASE = 0.98
_SCORE_NORMALIZED = 0.95
_SCORE_ALIAS = 0.90
_FUZZY_WEIGHT = 0.90  # fuzzy score = ratio/100 * weight

# Name stages that identify a variable on their own, even outside its documented scope: the names
# are the same string up to case/punctuation. Used for cross-file documentation and phantom checks.
STRONG_METHODS = frozenset({"exact", "case_insensitive", "normalized"})
# Name stages that are reliable inside a scope but too loose to bind documentation across files.
ALIAS_METHODS = frozenset({"readme_abbreviation", "token_set", "token_abbreviation"})
# Exact and case-only matches: the same name, so context nudges may not pull them below review.
SAME_NAME_METHODS = frozenset({"exact", "case_insensitive"})


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: float
    method: str
    evidence: tuple[str, ...] = ()

    @property
    def is_strong(self) -> bool:
        return self.method in STRONG_METHODS


def score_pair(
    column: str,
    documented: str,
    aliases: dict[str, str] | None = None,
) -> ScoreResult:
    """Best lexical score for one (column, documented-name) pair across stages 1-4.

    ``aliases`` maps a README short form to its long form (Schwartz-Hearst, Ref [23]); it lets an
    abbreviation in the data match a spelled-out name in the docs.
    """
    named = name_link(column, documented, aliases)
    if named is not None:
        return named

    # Stage 4: lexical fuzzy (Indel ratio on normalized forms).
    ratio = fuzz.ratio(normalize_name(column), normalize_name(documented))
    if ratio > 0:
        return ScoreResult(
            round(ratio / 100.0 * _FUZZY_WEIGHT, 4),
            "fuzzy",
            (f"{ratio:.0f}% lexical similarity",),
        )
    return ScoreResult(0.0, "none")


def name_link(
    column: str,
    documented: str,
    aliases: dict[str, str] | None = None,
) -> ScoreResult | None:
    """Stages 1-3 only (no fuzzy): a name-level link, or None if the names are merely similar.

    Cheap enough to run over every column of every dataset when deciding which dataset a README
    section describes (``linking.py``).
    """
    # Stage 1: exact string.
    if column == documented:
        return ScoreResult(_SCORE_EXACT, "exact", ("identical name",))

    # Stage 2a: case-insensitive (handles Date/date, Sr/SR, HP_GreenFeed/HP_Greenfeed).
    if column.lower() == documented.lower():
        return ScoreResult(_SCORE_CASE, "case_insensitive", ("differs only by letter case",))

    # Stage 2b: normalized (strip separators/accents/bracket suffix; milk-fat vs milk_fat).
    col_norm = normalize_name(column)
    if col_norm and col_norm == normalize_name(documented):
        return ScoreResult(_SCORE_NORMALIZED, "normalized", ("identical after normalising punctuation/case",))

    # Stage 3: token alias / abbreviation.
    return _alias_score(column, documented, aliases or {})


def strong_link(column: str, documented: str) -> ScoreResult | None:
    """A stage 1-2 link (same name up to case/punctuation), else None."""
    result = name_link(column, documented)
    return result if result is not None and result.is_strong else None


def _alias_score(column: str, documented: str, aliases: dict[str, str]) -> ScoreResult | None:
    """Stage 3: match via token equality, ordered abbreviation, or a declared README alias."""
    # Declared abbreviation: the documented short form expands to text equal to the column, or
    # vice versa (e.g. README says "Bacterial abundance (BA)", column is "BA").
    for short, long in aliases.items():
        if _eq(column, short) and _eq(long, documented):
            return ScoreResult(_SCORE_ALIAS, "readme_abbreviation", (f"'{short}' defined as '{long}'",))
        if _eq(documented, short) and _eq(long, column):
            return ScoreResult(_SCORE_ALIAS, "readme_abbreviation", (f"'{short}' defined as '{long}'",))

    col_tokens = tokenize(column)
    doc_tokens = tokenize(documented)
    if not col_tokens or not doc_tokens:
        return None

    # Token-set equality after normalisation (handles reordering of multi-word names).
    if set(col_tokens) == set(doc_tokens):
        return ScoreResult(_SCORE_ALIAS, "token_set", ("same tokens in a different order",))

    # Ordered-subsequence abbreviation per aligned token (BWsmooth_chg vs BWsmth_chg: smth ⊂ smooth).
    if len(col_tokens) == len(doc_tokens):
        evidence: list[str] = []
        for a, b in zip(col_tokens, doc_tokens):
            if a == b:
                continue
            if is_abbreviation(a, b):
                evidence.append(f"'{a}' abbreviates '{b}'")
            elif is_abbreviation(b, a):
                evidence.append(f"'{b}' abbreviates '{a}'")
            else:
                return None
        if evidence:
            return ScoreResult(_SCORE_ALIAS, "token_abbreviation", tuple(evidence))
    return None


def _eq(a: str, b: str) -> bool:
    return normalize_name(a) == normalize_name(b)


def is_abbreviation(short: str, long: str) -> bool:
    """True if ``short`` is an ordered subsequence of ``long`` sharing the first letter.

    Example: ``smth`` is an abbreviation of ``smooth`` (s-m-th all appear in order). Requires a
    length gap so trivially-similar tokens are not treated as abbreviations.
    """
    if len(short) < 2 or len(short) >= len(long) or short[0] != long[0]:
        return False
    i = 0
    for char in long:
        if i < len(short) and short[i] == char:
            i += 1
    return i == len(short)
