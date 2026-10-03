"""Assign documented variables to dataset columns.

Builds a score for every (column, documented-variable) pair, applies contextual adjustments
(instance/structure evidence, Ref [12]), then greedily assigns one-to-one in descending score with
deterministic tie-breaks (DESIGN.md section 14). Produces a ``VariableMatch`` per column and a list
of documented variables that matched no column.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import PipelineConfig
from ..readme.variable_layouts import VariableDefinition
from ..schema.document import (
    ColumnType,
    MatchAlternative,
    MatchStatus,
    UnmatchedVariable,
    VariableMatch,
)
from .normalize import normalize_name
from .scorers import ScoreResult, score_pair


@dataclass(slots=True)
class ColumnContext:
    """What we know about a column, used for contextual scoring adjustments (stage 5)."""

    name: str
    position: int
    column_type: ColumnType
    distinct_values: frozenset[str]
    distinct_count: int


@dataclass(slots=True)
class MatchOutcome:
    """Result of matching one dataset's columns to its documented variables."""

    matches: dict[str, VariableMatch]              # column name -> match
    unmatched_documented: list[UnmatchedVariable]  # documented but no column


def match_variables(
    columns: list[ColumnContext],
    definitions: list[VariableDefinition],
    aliases: dict[str, str],
    config: PipelineConfig,
) -> MatchOutcome:
    """Match a dataset's columns to its documented variables (DESIGN.md section 14)."""
    if not definitions:
        # Nothing documented: every column is unmatched (undocumented), reported by the caller.
        return MatchOutcome(
            matches={c.name: VariableMatch(status=MatchStatus.UNMATCHED) for c in columns},
            unmatched_documented=[],
        )

    # Build the full score grid: scores[col_index][def_index] -> (adjusted_score, ScoreResult).
    scores: list[list[tuple[float, ScoreResult]]] = []
    for col in columns:
        row: list[tuple[float, ScoreResult]] = []
        for defn in definitions:
            base = score_pair(col.name, defn.name, aliases)
            adjusted = _apply_context(base.score, col, defn, columns)
            row.append((adjusted, base))
        scores.append(row)

    # Greedy one-to-one assignment in descending score, deterministic tie-break
    # (score, column position, definition position).
    candidate_cells = [
        (scores[ci][di][0], ci, di)
        for ci in range(len(columns))
        for di in range(len(definitions))
        if scores[ci][di][0] > 0
    ]
    candidate_cells.sort(key=lambda cell: (-cell[0], columns[cell[1]].position, definitions[cell[2]].position))

    assigned_col: dict[int, int] = {}
    assigned_def: set[int] = set()
    for score, ci, di in candidate_cells:
        if ci in assigned_col or di in assigned_def:
            continue
        if score < config.match_review_threshold:
            break  # the rest are all below the review floor
        assigned_col[ci] = di
        assigned_def.add(di)

    matches = _build_matches(columns, definitions, scores, assigned_col, config)
    unmatched = [
        UnmatchedVariable(name=definitions[di].name, description=definitions[di].description,
                          lines=(definitions[di].line_start, definitions[di].line_end))
        for di in range(len(definitions))
        if di not in assigned_def
    ]
    return MatchOutcome(matches=matches, unmatched_documented=unmatched)


def _apply_context(
    base: float,
    col: ColumnContext,
    defn: VariableDefinition,
    columns: list[ColumnContext],
) -> float:
    """Stage 5: nudge the base score with instance/structure evidence (Ref [12]), capped at 1.0."""
    if base <= 0:
        return base
    score = base

    # Same ordinal position in the README list and the dataset (+/- 1).
    if defn.position and abs((col.position + 1) - defn.position) <= 1:
        score += 0.03

    # Documented value-label codes should appear among the observed distinct values.
    if defn.value_labels:
        codes = {normalize_name(v.code) for v in defn.value_labels}
        observed = {normalize_name(v) for v in col.distinct_values}
        if codes and codes <= observed:
            score += 0.03
        elif codes and codes.isdisjoint(observed):
            score -= 0.05

    # A documented unit implies a numeric column; reward agreement.
    if defn.unit and col.column_type in (ColumnType.NUMBER, ColumnType.INTEGER):
        score += 0.02

    return min(1.0, round(score, 4))


def _build_matches(
    columns: list[ColumnContext],
    definitions: list[VariableDefinition],
    scores: list[list[tuple[float, ScoreResult]]],
    assigned_col: dict[int, int],
    config: PipelineConfig,
) -> dict[str, VariableMatch]:
    matches: dict[str, VariableMatch] = {}
    for ci, col in enumerate(columns):
        if ci not in assigned_col:
            # Report the best candidates we rejected, so the user sees why it is unmatched.
            matches[col.name] = _unmatched_with_candidates(ci, definitions, scores)
            continue
        di = assigned_col[ci]
        score, base = scores[ci][di]

        # Ambiguity: is there another *unassigned* definition almost as good?
        alternatives = _alternatives(ci, di, definitions, scores, assigned_col)
        ambiguous = bool(alternatives) and (score - alternatives[0].confidence) < config.ambiguity_margin

        warning = None
        status = MatchStatus.MATCHED
        if ambiguous:
            status = MatchStatus.AMBIGUOUS
            warning = "Another documented variable scored almost as highly."
        elif score < config.match_accept_threshold:
            warning = "Low-confidence match; please verify."

        matches[col.name] = VariableMatch(
            status=status,
            documented_name=definitions[di].name,
            confidence=round(score, 4),
            method=base.method,
            evidence=list(base.evidence),
            alternatives=alternatives,
            warning=warning,
        )
    return matches


def _alternatives(
    ci: int,
    chosen_di: int,
    definitions: list[VariableDefinition],
    scores: list[list[tuple[float, ScoreResult]]],
    assigned_col: dict[int, int],
) -> list[MatchAlternative]:
    """Up to two next-best documented names for a column, for transparency."""
    others = [
        (scores[ci][di][0], di)
        for di in range(len(definitions))
        if di != chosen_di and scores[ci][di][0] > 0
    ]
    others.sort(key=lambda x: -x[0])
    return [MatchAlternative(documented_name=definitions[di].name, confidence=round(s, 4))
            for s, di in others[:2]]


def _unmatched_with_candidates(
    ci: int,
    definitions: list[VariableDefinition],
    scores: list[list[tuple[float, ScoreResult]]],
) -> VariableMatch:
    cands = [(scores[ci][di][0], di) for di in range(len(definitions)) if scores[ci][di][0] > 0]
    cands.sort(key=lambda x: -x[0])
    alts = [MatchAlternative(documented_name=definitions[di].name, confidence=round(s, 4))
            for s, di in cands[:3]]
    return VariableMatch(status=MatchStatus.UNMATCHED, alternatives=alts)
