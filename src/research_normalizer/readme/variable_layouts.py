"""Recognise variable definitions in a README section, whatever the layout.

Repositories describe variables in very different ways. We run several independent recognisers over a
section and keep the one that explains the most lines (DESIGN.md section 11). Each returns a list of
:class:`VariableDefinition` with line-range provenance. Recognisers implemented:

1. block        - ``Name: X`` then ``Description:``/``Units:``/``Value labels:``/``Notes:`` (dairy)
2. delimited    - ``X, description, unit`` on one line (pig)
3. table        - a pipe or aligned table whose header names the columns
4. dictionary   - a DOCX/CSV table of the same shape (handled via `table` on cells)
5. anchored     - any line whose first token matches a known dataset column (last-resort fallback)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schema.document import ValueLabel
from .segmenter import Block, BlockType, Section

# Keys used inside a block-style variable definition.
_NAME_KEYS = {"name", "variable", "variable name", "field"}
_DESC_KEYS = {"description", "label", "definition", "desc"}
_UNIT_KEYS = {"unit", "units", "unit of measurement", "measurement unit"}
_LABEL_KEYS = {"value labels", "value label", "codes", "categories", "levels"}
_NOTE_KEYS = {"notes", "note", "comment", "comments", "remarks"}

# Delimiters that separate name / description / unit on a single line.
_LINE_SPLIT = re.compile(r"\s*[,\u2013\-:=\t]\s*")  # comma, en-dash, hyphen, colon, equals, tab
_VALUE_LABEL_RE = re.compile(r"^\s*(?P<code>[\w.+-]+)\s*(?:=|:|\s-\s)\s*(?P<label>.+)$")
# A short, unit-like trailing segment (contains a unit cue). Used to peel a unit off a line.
_UNIT_CUE = re.compile(r"(/|%|\^|°|·|\bper\b|\-1\b|µ|mg|kg|cm|mm|km|ml|mol|ppm|ppb|°c|hour|day|yr|year)", re.IGNORECASE)
# Header synonyms for table/dictionary recognisers.
_HEADER_NAME = {"name", "variable", "variable name", "field", "column", "code"}
_HEADER_DESC = {"description", "label", "definition", "meaning"}
_HEADER_UNIT = {"unit", "units"}


@dataclass(slots=True)
class VariableDefinition:
    """A documented variable, with where in the README it was found."""

    name: str
    description: str | None = None
    unit: str | None = None
    value_labels: list[ValueLabel] = field(default_factory=list)
    notes: str | None = None
    position: int = 0                      # order within the README list
    line_start: int = 0
    line_end: int = 0
    method: str = ""                       # recogniser name, for provenance


def _norm_key(key: str) -> str:
    return key.strip().lower().rstrip(":")


def recognise_variables(
    section: Section,
    documented_columns: frozenset[str] = frozenset(),
) -> list[VariableDefinition]:
    """Return the best set of variable definitions found in a section."""
    candidates = [
        _block_layout(section),
        _delimited_layout(section),
        _table_layout(section),
    ]
    if documented_columns:
        candidates.append(_anchored_layout(section, documented_columns))
    # Keep the recogniser that produced the most definitions (ties: earliest in the list order,
    # which is also roughly most-specific first).
    best = max(candidates, key=len) if candidates else []
    return best


# --------------------------------------------------------------------------- recogniser 1: block
def _block_layout(section: Section) -> list[VariableDefinition]:
    defs: list[VariableDefinition] = []
    current: VariableDefinition | None = None
    position = 0
    for block in section.blocks:
        if block.type is not BlockType.KEY_VALUE or block.key is None:
            continue
        key = _norm_key(block.key)
        value = (block.value or "").strip()
        if key in _NAME_KEYS:
            if current is not None:
                defs.append(current)
            position += 1
            current = VariableDefinition(name=value, position=position,
                                         line_start=block.line_start, line_end=block.line_end,
                                         method="block")
        elif current is None:
            continue
        elif key in _DESC_KEYS:
            current.description = value or current.description
            current.line_end = block.line_end
        elif key in _UNIT_KEYS:
            current.unit = value or current.unit
            current.line_end = block.line_end
        elif key in _LABEL_KEYS:
            current.value_labels = _parse_value_labels(value)
            current.line_end = block.line_end
        elif key in _NOTE_KEYS:
            current.notes = value or current.notes
            current.line_end = block.line_end
    if current is not None:
        defs.append(current)
    # A block recogniser is only credible if it saw explicit Name: keys.
    return [d for d in defs if d.name]


# --------------------------------------------------------------------- recogniser 2: delimited line
def _delimited_layout(section: Section) -> list[VariableDefinition]:
    defs: list[VariableDefinition] = []
    position = 0
    for block in section.blocks:
        # Consider list items and plain text lines (the pig README variable list is plain lines).
        if block.type not in (BlockType.LIST_ITEM, BlockType.TEXT):
            continue
        line = block.text.strip()
        if not line or line.endswith(":"):
            continue
        parts = [p for p in _LINE_SPLIT.split(line) if p != ""]
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        if not name or " " in name and len(name.split()) > 4:
            continue  # a prose sentence, not "name, description"
        rest = parts[1:]
        unit = None
        # Peel a trailing unit-like segment off the end ("BR, bacterial respiration, ugC/gSoil/hour").
        # Only when something is left for the description, the segment contains a unit cue, and it
        # is short: a long last segment is more likely the end of a sentence than a unit.
        if len(rest) >= 2 and _UNIT_CUE.search(rest[-1]) and len(rest[-1]) <= 25:
            unit = rest[-1].strip()
            rest = rest[:-1]
        description = ", ".join(r.strip() for r in rest).strip() or None
        if description is None:
            continue
        position += 1
        defs.append(VariableDefinition(name=name, description=description, unit=unit, position=position,
                                       line_start=block.line_start, line_end=block.line_end,
                                       method="delimited_line"))
    return defs


# ------------------------------------------------------------------------- recogniser 3: table
def _table_layout(section: Section) -> list[VariableDefinition]:
    rows = [b for b in section.blocks if b.type is BlockType.TABLE_ROW]
    if len(rows) < 2:
        return []
    header = [c.strip().lower() for c in rows[0].cells]
    name_idx = _find_col(header, _HEADER_NAME)
    if name_idx is None:
        return []
    desc_idx = _find_col(header, _HEADER_DESC)
    unit_idx = _find_col(header, _HEADER_UNIT)

    defs: list[VariableDefinition] = []
    for position, row in enumerate(rows[1:], start=1):
        cells = row.cells
        if name_idx >= len(cells) or not cells[name_idx].strip():
            continue
        defs.append(VariableDefinition(
            name=cells[name_idx].strip(),
            description=cells[desc_idx].strip() if desc_idx is not None and desc_idx < len(cells) else None,
            unit=cells[unit_idx].strip() if unit_idx is not None and unit_idx < len(cells) else None,
            position=position, line_start=row.line_start, line_end=row.line_end, method="table",
        ))
    return defs


# ------------------------------------------------------------------- recogniser 4: column-anchored
def _anchored_layout(section: Section, columns: frozenset[str]) -> list[VariableDefinition]:
    """Last resort: any line whose first token matches a known column name."""
    from ..matching.normalize import normalize_name

    norm_cols = {normalize_name(c): c for c in columns}
    defs: list[VariableDefinition] = []
    position = 0
    for block in section.blocks:
        if block.type not in (BlockType.LIST_ITEM, BlockType.TEXT, BlockType.KEY_VALUE):
            continue
        text = block.text.strip()
        if not text:
            continue
        first = re.split(r"[\s,:=\-\t]", text, maxsplit=1)
        token = first[0].strip()
        if normalize_name(token) in norm_cols:
            position += 1
            rest = first[1].strip() if len(first) > 1 else None
            defs.append(VariableDefinition(name=token, description=rest or None, position=position,
                                           line_start=block.line_start, line_end=block.line_end,
                                           method="anchored"))
    return defs


# --------------------------------------------------------------------------- helpers
def _find_col(header: list[str], synonyms: set[str]) -> int | None:
    for i, name in enumerate(header):
        if name in synonyms:
            return i
    return None


def _parse_value_labels(value: str) -> list[ValueLabel]:
    """Parse '1 = Male, 2 = Female' / '0: no; 1: yes' / 'M - male' into code/label pairs."""
    if not value or value.lower() in {"n/a", "na", "none"}:
        return []
    labels: list[ValueLabel] = []
    for part in re.split(r"[;,]", value):
        m = _VALUE_LABEL_RE.match(part.strip())
        if m:
            labels.append(ValueLabel(code=m.group("code").strip(), label=m.group("label").strip()))
    return labels


# --------------------------------------------------------------------------- declared counts & codes
_COUNT_VAR_RE = re.compile(r"number of variables\s*[:=]?\s*(\d+)", re.IGNORECASE)
_COUNT_ROW_RE = re.compile(r"number of (?:cases|rows|observations)(?:/rows)?\s*[:=]?\s*(\d+)", re.IGNORECASE)
_MISSING_RE = re.compile(r"missing (?:data )?(?:codes?|values?)\s*[:=]?\s*(.+)", re.IGNORECASE)


def find_declared_counts(section: Section) -> tuple[int | None, int | None]:
    """Extract 'Number of variables' and 'Number of cases/rows' from a section."""
    text = "\n".join(b.text for b in section.blocks)
    var = _COUNT_VAR_RE.search(text)
    row = _COUNT_ROW_RE.search(text)
    return (int(var.group(1)) if var else None, int(row.group(1)) if row else None)


def find_missing_codes(section: Section) -> list[str]:
    """Extract declared missing-data codes from a section (e.g. '.', 'NA')."""
    for block in section.blocks:
        text = block.value if block.type is BlockType.KEY_VALUE and block.value else block.text
        m = _MISSING_RE.match(block.text) if block.type is not BlockType.KEY_VALUE else None
        if block.type is BlockType.KEY_VALUE and block.key and "missing" in block.key.lower():
            return _split_codes(block.value or "")
        if m:
            return _split_codes(m.group(1))
    return []


def _split_codes(value: str) -> list[str]:
    raw = re.split(r"[,;]|\bor\b|\s+", value.strip())
    return [c for c in (r.strip() for r in raw) if c and c.lower() not in {"and", ""}]
