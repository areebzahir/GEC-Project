"""Find which row is the header, and clean the header names.

Real files put titles, blank rows and unit rows above the header. Following the row-classification
idea in Pytheas (Ref [26]), we score each candidate row and pick the best: a header row tends to be
full, textual, unique, and sit above rows that are more numeric than itself (DESIGN.md section 12).
We then clean names: blanks become ``column_n`` and duplicates get numeric suffixes, always keeping
the originals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import PipelineConfig

_NUMERIC_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_DATE_RE = re.compile(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}$")
_UNIT_RE = re.compile(r"^[\(\[].+[\)\]]$|^[a-zA-Z%°/]+(\s*/\s*[a-zA-Z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class HeaderChoice:
    """The chosen header row (0-based index into the grid) and how sure we are."""

    row_index: int | None   # None => no good header, caller should synthesise names
    confidence: float
    ambiguous: bool


def _looks_numeric(cell: str) -> bool:
    return bool(_NUMERIC_RE.match(cell.strip()))


def _looks_date(cell: str) -> bool:
    return bool(_DATE_RE.match(cell.strip()))


def _row_textuality(row: list[str]) -> float:
    """Fraction of non-empty cells that are neither numeric nor dates (headers are textual)."""
    filled = [c for c in row if c.strip()]
    if not filled:
        return 0.0
    textual = sum(1 for c in filled if not _looks_numeric(c) and not _looks_date(c))
    return textual / len(filled)


def _fill_ratio(row: list[str], width: int) -> float:
    if width == 0:
        return 0.0
    return sum(1 for c in row if c.strip()) / width


def _uniqueness(row: list[str]) -> float:
    filled = [c.strip() for c in row if c.strip()]
    if not filled:
        return 0.0
    return len(set(filled)) / len(filled)


def _modal_width(grid: list[list[str]]) -> int:
    counts: dict[int, int] = {}
    for row in grid:
        counts[len(row)] = counts.get(len(row), 0) + 1
    return max(counts, key=lambda w: (counts[w], w)) if counts else 0


def detect_header(
    grid: list[list[str]],
    config: PipelineConfig,
    documented_names: frozenset[str] = frozenset(),
) -> HeaderChoice:
    """Score the first ``header_scan_rows`` rows and return the best header candidate.

    ``documented_names`` (normalised README variable names) gives the row that overlaps the
    documentation a boost: instance-level evidence that it is the header (DESIGN.md section 12).
    """
    if not grid:
        return HeaderChoice(None, 0.0, False)

    width = _modal_width(grid)
    scan = min(len(grid), config.header_scan_rows)
    scored: list[tuple[float, int]] = []

    for i in range(scan):
        row = grid[i]
        filled = [c for c in row if c.strip()]
        if not filled:
            continue  # blank line, never a header

        fill = _fill_ratio(row, width)
        textual = _row_textuality(row)
        unique = _uniqueness(row)

        # Type contrast: how much more numeric are the rows *below* this one?
        below = grid[i + 1 : i + 6]
        below_numeric = _numeric_fraction(below)
        this_numeric = sum(1 for c in filled if _looks_numeric(c)) / len(filled)
        contrast = max(0.0, below_numeric - this_numeric)

        overlap = 0.0
        if documented_names:
            norm = {re.sub(r"[\s_\-.]+", "", c.strip().lower()) for c in filled}
            overlap = len(norm & documented_names) / len(filled)

        # Weighted evidence that row i is the header (Pytheas-style, DESIGN.md section 12):
        #   fill     - a header spans the table's full width; title/note rows usually don't
        #   textual  - header cells are words, not numbers or dates
        #   unique   - header names don't repeat; repeated values look like data
        #   contrast - the rows below are clearly more numeric than this one (header -> data switch)
        #   overlap  - cells match README variable names; the strongest single clue when available
        # The weights add up to more than 1 on purpose: overlap is a bonus, and only the ranking and
        # the margin to the runner-up are used.
        score = (
            0.30 * fill
            + 0.25 * textual
            + 0.15 * unique
            + 0.20 * contrast
            + 0.25 * overlap
        )
        # Penalise rows that are basically a single cell (titles) or look like a units row.
        if len(filled) == 1:
            score -= 0.4
        if all(_UNIT_RE.match(c.strip()) for c in filled) and i > 0:
            score -= 0.2

        scored.append((score, i))

    if not scored:
        return HeaderChoice(None, 0.0, False)

    scored.sort(key=lambda s: (-s[0], s[1]))
    best_score, best_idx = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0

    if best_score < config.header_min_score:
        return HeaderChoice(None, round(max(best_score, 0.0), 3), False)

    # Normalised margin to the runner-up gives a confidence and an ambiguity flag.
    margin = best_score - second_score
    ambiguous = margin < 0.05 and best_score > 0
    confidence = round(min(1.0, 0.5 + margin), 3)
    return HeaderChoice(best_idx, confidence, ambiguous)


def _numeric_fraction(rows: list[list[str]]) -> float:
    cells = [c for row in rows for c in row if c.strip()]
    if not cells:
        return 0.0
    return sum(1 for c in cells if _looks_numeric(c) or _looks_date(c)) / len(cells)


def clean_headers(raw: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (clean names, notes) where notes are (kind, detail) describing each repair.

    * blank  -> ``column_<n>`` (1-based position)
    * duplicate -> ``name__2``, ``name__3`` …
    * surrounding whitespace is stripped
    The originals are preserved by the caller alongside these cleaned names.
    """
    notes: list[tuple[str, str]] = []
    cleaned: list[str] = []
    seen: dict[str, int] = {}
    for position, name in enumerate(raw, start=1):
        value = name.strip()
        if not value:
            value = f"column_{position}"
            notes.append(("blank", value))
        if value in seen:
            seen[value] += 1
            new_value = f"{value}__{seen[value]}"
            notes.append(("duplicate", f"{value} -> {new_value}"))
            value = new_value
        else:
            seen[value] = 1
        cleaned.append(value)
    return cleaned, notes
