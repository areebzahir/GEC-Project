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
# A whole cell wrapped in brackets: "(kg)", "[mg/L]".
_BRACKETED_RE = re.compile(r"^[\(\[](?P<inner>[^\(\)\[\]]{1,20})[\)\]]$")
# A trailing bracketed part of a header name: "Temperature (°C)", "Mass [kg]".
_HEADER_UNIT_RE = re.compile(r"[\(\[](?P<inner>[^\(\)\[\]]{1,20})[\)\]]\s*$")
# Characters that only appear in unit notation (ratio, percent, degree, micro, powers).
_UNIT_SYMBOL_RE = re.compile(r"[/%°µμ^²³‰]")
# Bare unit words recognised without symbols. Multi-letter only: single letters ("m", "g", "s")
# are too often category codes (sex, grade) to be trusted on their own.
_BARE_UNITS = frozenset({
    "kg", "mg", "ug", "ng", "mm", "cm", "km", "ml", "ul", "mol", "mmol", "umol", "ppm", "ppb",
    "pa", "kpa", "hpa", "mpa", "kj", "mj", "kcal", "cal", "kw", "mw", "ha", "degc", "degf",
    "sec", "secs", "seconds", "minutes", "hr", "hrs", "hours", "days", "weeks", "months",
    "yr", "yrs", "year", "years", "percent", "lb", "lbs", "oz", "ft", "inch", "inches",
})
_MAX_UNIT_CHARS = 20
# Share of a row's informative cells that must look like units for it to be a units row.
_UNITS_ROW_MIN_SHARE = 0.6
# Placeholders meaning "no unit" in a units row; neither unit evidence nor counter-evidence.
_NO_UNIT_PLACEHOLDERS = frozenset({"", "-", "\u2014", "na", "n/a", "none", "unitless", "()"})


@dataclass(frozen=True, slots=True)
class HeaderChoice:
    """The chosen header row (0-based index into the grid) and how sure we are."""

    row_index: int | None   # None => no good header, caller should synthesise names
    confidence: float
    ambiguous: bool


def looks_numeric(cell: str) -> bool:
    """True for a plain number such as ``12``, ``-3.5`` or ``9.16E+06``."""
    return bool(_NUMERIC_RE.match(cell.strip()))



def _unit_like_token(text: str) -> bool:
    """True if ``text`` (without brackets) reads like unit notation: "mg/L", "°C", "%", "kg"."""
    t = text.strip()
    if not t or len(t) > _MAX_UNIT_CHARS or looks_numeric(t) or len(t.split()) > 3:
        return False
    # Units are mostly letters/symbols; digit-heavy text with "/" is a date or ratio value.
    if sum(ch.isdigit() for ch in t) > sum(ch.isalpha() for ch in t):
        return False
    return bool(_UNIT_SYMBOL_RE.search(t)) or t.lower() in _BARE_UNITS


def looks_like_unit(cell: str) -> bool:
    """True for a units-row cell: a short bracketed token like "(kg)" or unit notation like "mg/L"."""
    t = cell.strip()
    bracketed = _BRACKETED_RE.match(t)
    if bracketed:
        # Any short bracketed token that is not number-like is a unit in a units row: "(n)", "(score)".
        inner = bracketed.group("inner")
        return sum(ch.isdigit() for ch in inner) <= sum(ch.isalpha() for ch in inner)
    return _unit_like_token(t)


def is_units_row(row: list[str]) -> bool:
    """True if a row (usually the one right under the header) holds units rather than data.

    Placeholders such as "-" or blank (columns without a unit) are ignored; any plain number means
    it is data. Most of the remaining cells must look like units.
    """
    informative = [c.strip() for c in row if c.strip().lower() not in _NO_UNIT_PLACEHOLDERS]
    if not informative or any(looks_numeric(c) for c in informative):
        return False
    units = sum(1 for c in informative if looks_like_unit(c))
    return units / len(informative) >= _UNITS_ROW_MIN_SHARE


def unit_from_units_cell(cell: str) -> str | None:
    """The unit text of a units-row cell, brackets removed ("(kg)" -> "kg"); None for placeholders."""
    t = cell.strip()
    if t.lower() in _NO_UNIT_PLACEHOLDERS:
        return None
    bracketed = _BRACKETED_RE.match(t)
    return bracketed.group("inner").strip() if bracketed else t


def collect_header_units(
    columns: list[str],
    original_headers: list[str],
    units_row: list[str] | None,
) -> dict[str, str]:
    """Build ``{column: unit}`` from a units row (preferred) or units embedded in header names."""
    units: dict[str, str] = {}
    for i, column in enumerate(columns):
        unit = None
        if units_row is not None and i < len(units_row):
            unit = unit_from_units_cell(units_row[i])
        if unit is None and i < len(original_headers):
            unit = unit_from_header(original_headers[i])
        if unit:
            units[column] = unit
    return units


def unit_from_header(name: str) -> str | None:
    """Unit embedded in a header name: "Temperature (°C)" -> "°C". None if the bracket is not a unit.

    The name itself is never changed; this only feeds ``RawTable.header_units``.
    """
    m = _HEADER_UNIT_RE.search(name.strip())
    if not m or m.start() == 0:
        return None  # no bracket, or the whole name is bracketed (that is a units cell, not a name)
    inner = m.group("inner").strip()
    return inner if _unit_like_token(inner) else None


def _looks_date(cell: str) -> bool:
    return bool(_DATE_RE.match(cell.strip()))


def _row_textuality(row: list[str]) -> float:
    """Fraction of non-empty cells that are neither numeric nor dates (headers are textual)."""
    filled = [c for c in row if c.strip()]
    if not filled:
        return 0.0
    textual = sum(1 for c in filled if not looks_numeric(c) and not _looks_date(c))
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
        this_numeric = sum(1 for c in filled if looks_numeric(c)) / len(filled)
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
        if i > 0 and is_units_row(row):
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
    return sum(1 for c in cells if looks_numeric(c) or _looks_date(c)) / len(cells)


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
