"""Assign documented variables to dataset columns.

Builds a score for every (column, documented-variable) pair, applies contextual adjustments
(instance/structure evidence, Ref [12]), then greedily assigns one-to-one in descending score with
deterministic tie-breaks (DESIGN.md section 14). Confidence is then calibrated:

* exact and case-only name matches never drop below ``SAME_NAME_FLOOR``;
* a *mutual-best* pair (the column's best definition and the definition's best column) that beats
  every free rival by ``UNIQUE_MARGIN`` and has no contradicting context gets ``UNIQUENESS_BOOST``;
* a pair whose rival is within ``config.ambiguity_margin`` is marked ambiguous.

Produces a ``VariableMatch`` per column and the documented variables that matched no column.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import PipelineConfig
from ..readme.variable_layouts import VariableDefinition
from ..schema.document import MatchAlternative, MatchStatus, UnmatchedVariable, VariableMatch
from .context import ColumnContext, Nudge, context_nudges, total_delta
from .scorers import SAME_NAME_METHODS, ScoreResult, score_pair

__all__ = ["ColumnContext", "MatchOutcome", "match_variables"]

# The same name (up to letter case) inside the documented scope is reliable on its own; context
# nudges may lower it a little but never into the review band.
SAME_NAME_FLOOR = 0.95
# A mutual-best pair must beat its closest free rival by this much to earn the uniqueness boost.
UNIQUE_MARGIN = 0.15
UNIQUENESS_BOOST = 0.05
# Only an exact name reaches 1.0; every inferred link (case, punctuation, abbreviation, fuzzy) stays
# at or below this, so confidence still tells the methods apart.
INFERRED_CEILING = 0.98
# Pairs whose name score is below this never get contextual nudges: context cannot lift them into
# the review band in a meaningful way, and skipping them keeps the grid cheap on wide tables.
_CONTEXT_MIN_BASE = 0.50


@dataclass(slots=True)
class MatchOutcome:
    """Result of matching one dataset's columns to its documented variables."""

    matches: dict[str, VariableMatch]              # column name -> match
    unmatched_documented: list[UnmatchedVariable]  # documented but no column
    assigned: dict[str, int] = field(default_factory=dict)       # column name -> definition index
    unmatched_indices: list[int] = field(default_factory=list)   # definition indices with no column


@dataclass(slots=True)
class _Cell:
    """One scored (column, definition) pair."""

    score: float
    base: ScoreResult
    nudges: list[Nudge]

    @property
    def contradicted(self) -> bool:
        return any(n.delta < 0 for n in self.nudges)


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

    grid = [[_score_cell(col, defn, aliases, config) for defn in definitions] for col in columns]
    assigned_col = _greedy_assign(columns, definitions, grid, config)
    owner_of_def = {di: ci for ci, di in assigned_col.items()}

    matches: dict[str, VariableMatch] = {}
    for ci, col in enumerate(columns):
        if ci in assigned_col:
            matches[col.name] = _assigned_match(ci, assigned_col[ci], definitions, grid,
                                                assigned_col, owner_of_def, config)
        else:
            # Report the best candidates we rejected, so the user sees why it is unmatched.
            matches[col.name] = _unmatched_with_candidates(ci, definitions, grid)

    unmatched_idx = [di for di in range(len(definitions)) if di not in owner_of_def]
    unmatched = [
        UnmatchedVariable(name=definitions[di].name, description=definitions[di].description,
                          lines=(definitions[di].line_start, definitions[di].line_end))
        for di in unmatched_idx
    ]
    return MatchOutcome(
        matches=matches, unmatched_documented=unmatched,
        assigned={columns[ci].name: di for ci, di in assigned_col.items()},
        unmatched_indices=unmatched_idx,
    )


def _score_cell(col: ColumnContext, defn: VariableDefinition, aliases: dict[str, str],
                config: PipelineConfig) -> _Cell:
    base = score_pair(col.name, defn.name, aliases)
    if base.score < _CONTEXT_MIN_BASE:
        return _Cell(base.score, base, [])
    nudges = context_nudges(col, defn, config.categorical_max_distinct)
    ceiling = 1.0 if base.method == "exact" else INFERRED_CEILING
    score = min(ceiling, max(0.0, base.score + total_delta(nudges)))
    if base.method in SAME_NAME_METHODS:
        score = max(score, SAME_NAME_FLOOR)
    return _Cell(round(score, 4), base, nudges)


def _greedy_assign(columns, definitions, grid, config) -> dict[int, int]:
    """Greedy one-to-one assignment in descending score (column index -> definition index).

    Greedy rather than Hungarian on purpose (DESIGN.md section 14): every link can be explained as
    "this was the best pair still free", e.g. the exact BW_smooth pair is taken before BWsmooth_chg
    can claim BW_smooth (84.2 fuzzy). Dense conflicts where greedy is sub-optimal are the ones the
    rival check flags as ambiguous. Ties break on column position, then README order.
    """
    cells = [
        (grid[ci][di].score, ci, di)
        for ci in range(len(columns))
        for di in range(len(definitions))
        if grid[ci][di].score >= config.match_review_threshold
    ]
    cells.sort(key=lambda c: (-c[0], columns[c[1]].position, definitions[c[2]].position))
    assigned_col: dict[int, int] = {}
    taken_defs: set[int] = set()
    for _, ci, di in cells:
        if ci not in assigned_col and di not in taken_defs:
            assigned_col[ci] = di
            taken_defs.add(di)
    return assigned_col


def _assigned_match(ci, di, definitions, grid, assigned_col, owner_of_def, config) -> VariableMatch:
    cell = grid[ci][di]
    rival = max(_row_rival(ci, di, grid, owner_of_def), _column_rival(ci, di, grid, assigned_col))
    margin = cell.score - rival
    evidence = [*cell.base.evidence, *(n.evidence for n in cell.nudges)]
    score = cell.score

    ambiguous = rival > 0 and margin < config.ambiguity_margin
    if (not ambiguous and margin >= UNIQUE_MARGIN and not cell.contradicted
            and cell.base.score >= config.match_accept_threshold and score < INFERRED_CEILING):
        score = min(INFERRED_CEILING, score + UNIQUENESS_BOOST)
        evidence.append(f"mutual best match; the closest free rival scores {rival:.2f}" if rival
                        else "mutual best match; no other candidate is free")

    warning = None
    status = MatchStatus.MATCHED
    if ambiguous:
        status = MatchStatus.AMBIGUOUS
        warning = "Another documented variable scored almost as highly."
        evidence.append(f"a rival candidate is within {config.ambiguity_margin} of this score")
    elif score < config.match_accept_threshold:
        warning = "Low-confidence match; please verify."

    return VariableMatch(
        status=status,
        documented_name=definitions[di].name,
        confidence=round(score, 4),
        method=cell.base.method,
        evidence=evidence,
        alternatives=_alternatives(ci, di, definitions, grid, owner_of_def),
        warning=warning,
    )


def _is_free(cell_score: float, owner_score: float | None) -> bool:
    """A candidate is a real rival unless a strictly better pair already owns it."""
    return owner_score is None or owner_score <= cell_score


def _row_rival(ci, di, grid, owner_of_def) -> float:
    """Best score of another definition that could still claim this column."""
    best = 0.0
    for dj, cell in enumerate(grid[ci]):
        owner = owner_of_def.get(dj)
        owner_score = grid[owner][dj].score if owner is not None and owner != ci else None
        if dj != di and _is_free(cell.score, owner_score):
            best = max(best, cell.score)
    return best


def _column_rival(ci, di, grid, assigned_col) -> float:
    """Best score of another column that could still claim this definition."""
    best = 0.0
    for cj in range(len(grid)):
        if cj == ci:
            continue
        own = assigned_col.get(cj)
        owner_score = grid[cj][own].score if own is not None else None
        if _is_free(grid[cj][di].score, owner_score):
            best = max(best, grid[cj][di].score)
    return best


def _alternatives(ci, chosen_di, definitions, grid, owner_of_def) -> list[MatchAlternative]:
    """Up to two next-best free documented names for a column, for transparency."""
    others = []
    for di, cell in enumerate(grid[ci]):
        owner = owner_of_def.get(di)
        owner_score = grid[owner][di].score if owner is not None and owner != ci else None
        if di != chosen_di and cell.score > 0 and _is_free(cell.score, owner_score):
            others.append((cell.score, di))
    others.sort(key=lambda x: -x[0])
    return [MatchAlternative(documented_name=definitions[di].name, confidence=round(s, 4))
            for s, di in others[:2]]


def _unmatched_with_candidates(ci, definitions, grid) -> VariableMatch:
    cands = sorted(((cell.score, di) for di, cell in enumerate(grid[ci]) if cell.score > 0),
                   key=lambda x: -x[0])
    alts = [MatchAlternative(documented_name=definitions[di].name, confidence=round(s, 4))
            for s, di in cands[:3]]
    return VariableMatch(status=MatchStatus.UNMATCHED, alternatives=alts)
