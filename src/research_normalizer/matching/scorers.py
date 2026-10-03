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


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: float
    method: str
    evidence: tuple[str, ...] = ()


def score_pair(
    column: str,
    documented: str,
    aliases: dict[str, str] | None = None,
) -> ScoreResult:
    """Best lexical score for one (column, documented-name) pair across stages 1-4.

    ``aliases`` maps a README short form to its long form (Schwartz-Hearst, Ref [23]); it lets an
    abbreviation in the data match a spelled-out name in the docs.
    """
    # Stage 1: exact string.
    if column == documented:
        return ScoreResult(_SCORE_EXACT, "exact")

    # Stage 2a: case-insensitive (handles Date/date, Sr/SR, HP_GreenFeed/HP_Greenfeed).
    if column.lower() == documented.lower():
        return ScoreResult(_SCORE_CASE, "case_insensitive", ("differs only by letter case",))

    col_norm = normalize_name(column)
    doc_norm = normalize_name(documented)

    # Stage 2b: normalized (strip separators/accents/bracket suffix; milk-fat vs milk_fat).
    if col_norm and col_norm == doc_norm:
        return ScoreResult(_SCORE_NORMALIZED, "normalized", ("identical after normalising punctuation/case",))

    # Stage 3: token alias / abbreviation.
    alias_result = _alias_score(column, documented, aliases or {})
    if alias_result is not None:
        return alias_result

    # Stage 4: lexical fuzzy (Indel ratio on normalized forms).
    ratio = fuzz.ratio(col_norm, doc_norm)
    if ratio > 0:
        return ScoreResult(
            round(ratio / 100.0 * _FUZZY_WEIGHT, 4),
            "fuzzy",
            (f"{ratio:.0f}% lexical similarity",),
        )
    return ScoreResult(0.0, "none")


def _alias_score(column: str, documented: str, aliases: dict[str, str]) -> ScoreResult | None:
    """Stage 3: match via token equality, ordered abbreviation, or a declared README alias."""
    # Declared abbreviation: the documented short form expands to text equal to the column, or
    # vice versa (e.g. README says "Bacterial abundance (BA)", column is "BA").
    for short, long in aliases.items():
        if _eq(column, short) and _eq(documented_norm := normalize_name(long), normalize_name(documented)):
            return ScoreResult(_SCORE_ALIAS, "readme_abbreviation", (f"'{short}' defined as '{long}'",))
        if _eq(documented, short) and normalize_name(long) == normalize_name(column):
            return ScoreResult(_SCORE_ALIAS, "readme_abbreviation", (f"'{short}' defined as '{long}'",))

    col_tokens = tokenize(column)
    doc_tokens = tokenize(documented)
    if not col_tokens or not doc_tokens:
        return None

    # Token-set equality after normalisation (handles reordering, e.g. Phase/Distance order swap at
    # the token level is irrelevant for single tokens but matters for multi-word names).
    if set(col_tokens) == set(doc_tokens):
        return ScoreResult(_SCORE_ALIAS, "token_set", ("same tokens in a different order",))

    # Ordered-subsequence abbreviation per aligned token (BWsmooth_chg vs BWsmth_chg: smth ⊂ smooth).
    if len(col_tokens) == len(doc_tokens):
        evidence: list[str] = []
        ok = True
        for a, b in zip(col_tokens, doc_tokens):
            if a == b:
                continue
            if _is_abbrev(a, b):
                evidence.append(f"'{a}' abbreviates '{b}'")
            elif _is_abbrev(b, a):
                evidence.append(f"'{b}' abbreviates '{a}'")
            else:
                ok = False
                break
        if ok and evidence:
            return ScoreResult(_SCORE_ALIAS, "token_abbreviation", tuple(evidence))
    return None


def _eq(a: str, b: str) -> bool:
    return normalize_name(a) == normalize_name(b)


def _is_abbrev(short: str, long: str) -> bool:
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
