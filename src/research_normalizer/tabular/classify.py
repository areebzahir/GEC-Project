"""Content-based classification: is this text a table, prose documentation, or a data dictionary?

File extensions lie (a ``.txt`` can be a tab-delimited table; a ``.csv`` can be a codebook), so
every decision here looks at the content itself. All rules are generic shape heuristics (row-width
consistency, cell length, share of numbers); nothing depends on a particular dataset's wording.
"""

from __future__ import annotations

import csv
import io
import re

from ..config import PipelineConfig
from .dialect import sniff_dialect
from .header_detection import looks_numeric

# --- sniff_text_role thresholds --------------------------------------------------------------
# A table needs at least this many rows of the modal width; fewer is too little evidence.
_MIN_TABLE_ROWS = 3
# Share of rows (after the preamble) that must have the modal width. Real tables are near 1.0;
# documents whose lines happen to contain commas sit far below.
_MIN_ROW_CONSISTENCY = 0.8
# Title/notes blocks above a data header are short. A README that *ends* with a delimited variable
# list has a long prose head, so a big preamble means "document that contains a table".
_MAX_PREAMBLE_ROWS = 10
# A cell with this many words reads as a sentence, not a data value.
_PROSE_CELL_WORDS = 8
# If more than this share of rows carry a sentence-length cell, the text is prose split by commas
# (unless the first column is identifier-like, as in a variable/description codebook).
_MAX_PROSE_ROW_SHARE = 0.5
# "Key: value" lines (metadata documents). The key may not contain a delimiter, and the colon must
# be followed by whitespace so times like 12:30 and URLs like http://x do not count.
_KEY_VALUE_RE = re.compile(r"^\s*(\d+[.)]\s*)?[^,\t;|:]{1,60}:\s+\S")
_MAX_KEY_VALUE_SHARE = 0.5
# Markdown table rule ("|---|:---:|"): the pipes form a table *inside a document*, not a data file.
_MARKDOWN_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
# Lines to look at; enough to judge shape, cheap on huge files.
_SNIFF_MAX_LINES = 200

# --- data-dictionary header vocabulary (generic words, compared after normalisation) ----------
# Strong: on their own these clearly name "the variable being described".
_STRONG_NAME_HEADERS = frozenset({
    "variable", "variable name", "variables", "var", "var name", "varname", "variable code",
    "field", "field name", "column", "column name", "attribute", "attribute name",
})
# Weak: common in dictionaries but also in ordinary data tables, so they need extra evidence.
_WEAK_NAME_HEADERS = frozenset({"name", "code", "item", "short name"})
_STRONG_DESCRIBE_WORDS = frozenset({"description", "definition", "meaning", "explanation", "desc"})
_WEAK_DESCRIBE_WORDS = frozenset({"label", "details", "notes"})
# Further columns that only make sense in a codebook (they describe a variable's properties).
_DICTIONARY_EXTRA_WORDS = frozenset({
    "unit", "units", "type", "format", "values", "range", "missing", "scale", "measurement",
})
# Identifier-like names: short and with few spaces ("body_mass", "Soil pH").
_MAX_NAME_WORDS = 4
_MAX_NAME_CHARS = 64
_MIN_IDENTIFIER_SHARE = 0.8

# --- notes-sheet thresholds -------------------------------------------------------------------
_MAX_NOTES_NUMERIC_SHARE = 0.2   # notes are words; data sheets are mostly numbers
_MIN_NOTES_MEAN_WORDS = 2.5      # a single column of short codes is a data list, not notes
_MIN_SINGLE_CELL_ROW_SHARE = 0.7 # "README pasted into column A"
_MIN_SENTENCE_ROW_SHARE = 0.3    # rows carrying a sentence-length cell


def _words(cell: str) -> int:
    return len(cell.split())


def _normalise_header(name: str) -> str:
    """'Variable_Name ' -> 'variable name' so synonyms compare on lowercase tokens."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", name.lower()).split())


# ============================================================================ sniff_text_role
def tabular_evidence(text: str) -> float:
    """How strongly ``text`` looks like a delimited table: 0.0 (document) .. 1.0 (table).

    0.5 means "consistent columns but too few rows to be sure"; the caller breaks that tie (e.g. by
    file name). :func:`sniff_text_role` is the two-valued wrapper of this.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()][:_SNIFF_MAX_LINES]
    if not lines:
        return 0.0
    if sum(1 for ln in lines if _KEY_VALUE_RE.match(ln)) / len(lines) > _MAX_KEY_VALUE_SHARE:
        return 0.0  # metadata document ("Title: ...", "Author: ...")
    if any(_MARKDOWN_TABLE_RULE_RE.match(ln) for ln in lines):
        return 0.0  # markdown document

    sample = "\n".join(lines)
    dialect = sniff_dialect(sample, "", PipelineConfig())
    if dialect.field_count < 2:
        return 0.0  # no delimiter splits the lines into columns
    rows = _parse(sample, dialect.delimiter, dialect.quote_char)
    if dialect.skip_rows > _MAX_PREAMBLE_ROWS:
        return 0.0  # a long document that happens to contain a table
    body = rows[dialect.skip_rows:]
    modal_rows = [r for r in body if len(r) == dialect.field_count]
    if not body or len(modal_rows) / len(body) < _MIN_ROW_CONSISTENCY:
        return 0.0
    if _is_prose_split_by_delimiter(modal_rows):
        return 0.0
    if len(modal_rows) < _MIN_TABLE_ROWS:
        return 0.5
    return 1.0


def sniff_text_role(text: str) -> str:
    """Return ``"tabular"`` or ``"documentation"`` for decoded text content.

    A consistent multi-column delimited structure over enough rows -> "tabular"; prose, key/value
    metadata and headings -> "documentation". A tiny but consistent table (e.g. header + one row)
    counts as tabular here; callers with a file name can use :func:`tabular_evidence` to break ties.
    """
    return "tabular" if tabular_evidence(text) >= 0.5 else "documentation"


def _parse(sample: str, delimiter: str, quote_char: str | None) -> list[list[str]]:
    kwargs: dict = {"delimiter": delimiter}
    if quote_char is None:
        kwargs["quoting"] = csv.QUOTE_NONE
    else:
        kwargs["quotechar"] = quote_char
    try:
        return [r for r in csv.reader(io.StringIO(sample), **kwargs) if r]
    except csv.Error:
        return [ln.split(delimiter) for ln in sample.splitlines() if ln.strip()]


def _is_prose_split_by_delimiter(rows: list[list[str]]) -> bool:
    """True if most rows carry sentence-length cells and the first column is not identifier-like.

    Sentences with a comma or two can split into a "consistent" width. A codebook CSV
    (``age,Age of the respondent in years``) also has long cells, but its first column is a short
    identifier, so it stays tabular.
    """
    prose_rows = sum(1 for r in rows if max(_words(c) for c in r) >= _PROSE_CELL_WORDS)
    if prose_rows / len(rows) <= _MAX_PROSE_ROW_SHARE:
        return False
    return not _mostly_identifiers([r[0] for r in rows])


def _mostly_identifiers(cells: list[str]) -> bool:
    filled = [c.strip() for c in cells if c.strip()]
    if not filled:
        return False
    ok = sum(1 for c in filled if len(c) <= _MAX_NAME_CHARS and _words(c) <= _MAX_NAME_WORDS)
    return ok / len(filled) >= _MIN_IDENTIFIER_SHARE


# ======================================================================= data dictionary
def _dictionary_columns(headers: list[str]) -> tuple[int, int] | None:
    """Find (name column, description column) indices if the header reads like a codebook."""
    norm = [_normalise_header(h) for h in headers]
    tokens = [set(n.split()) for n in norm]

    def describe_strength(i: int) -> int:
        if tokens[i] & _STRONG_DESCRIBE_WORDS:
            return 2
        return 1 if tokens[i] & _WEAK_DESCRIBE_WORDS else 0

    has_extra = any(t & _DICTIONARY_EXTRA_WORDS for t in tokens)
    for name_idx, n in enumerate(norm):
        strong_name = n in _STRONG_NAME_HEADERS
        if not strong_name and n not in _WEAK_NAME_HEADERS:
            continue
        for desc_idx in range(len(headers)):
            if desc_idx == name_idx:
                continue
            strength = describe_strength(desc_idx)
            # Weak name headers ("name", "code") need a strong describer or a codebook-only column.
            if strength and (strong_name or strength == 2 or has_extra):
                return name_idx, desc_idx
    return None


def looks_like_data_dictionary(headers: list[str], rows: list[list[str]]) -> bool:
    """True if a table describes variables (name + description/label/unit columns) rather than data.

    ``headers`` are the cleaned header names; ``rows`` a sample of body rows as strings. Header
    vocabulary alone is not enough: the name column's cells must also look like identifiers (short,
    few spaces, not numbers), which rules out e.g. a ``code,label`` lookup table of numeric codes.
    """
    cols = _dictionary_columns(headers)
    if cols is None:
        return False
    name_idx, desc_idx = cols
    names = [r[name_idx].strip() for r in rows if len(r) > name_idx and r[name_idx].strip()]
    if not names:
        return False
    if sum(1 for c in names if looks_numeric(c)) / len(names) > 1 - _MIN_IDENTIFIER_SHARE:
        return False
    if not _mostly_identifiers(names):
        return False
    descriptions = [r[desc_idx].strip() for r in rows if len(r) > desc_idx and r[desc_idx].strip()]
    # Most variables should actually be described, and descriptions are words, not numbers.
    if len(descriptions) < 0.5 * len(names):
        return False
    return sum(1 for c in descriptions if looks_numeric(c)) / len(descriptions) < 0.5


# ============================================================================ notes sheet
def looks_like_notes_sheet(grid: list[list[str]]) -> bool:
    """True if a spreadsheet grid is free-text notes/README content rather than a data table.

    Two generic shapes: (a) text written down one column (most rows have a single filled cell), or
    (b) rows carrying sentence-length cells with no consistent width. Both require few numbers.
    """
    rows = [[c.strip() for c in r if c and c.strip()] for r in grid]
    rows = [r for r in rows if r]
    cells = [c for r in rows for c in r]
    if not cells:
        return False
    numeric_share = sum(1 for c in cells if looks_numeric(c)) / len(cells)
    if numeric_share > _MAX_NOTES_NUMERIC_SHARE:
        return False

    mean_words = sum(_words(c) for c in cells) / len(cells)
    single_cell_share = sum(1 for r in rows if len(r) == 1) / len(rows)
    if single_cell_share >= _MIN_SINGLE_CELL_ROW_SHARE and mean_words >= _MIN_NOTES_MEAN_WORDS:
        return True

    sentence_share = sum(1 for r in rows if max(_words(c) for c in r) >= _PROSE_CELL_WORDS) / len(rows)
    return sentence_share >= _MIN_SENTENCE_ROW_SHARE and _width_consistency(rows) < _MIN_ROW_CONSISTENCY


def _width_consistency(rows: list[list[str]]) -> float:
    """Share of rows whose filled-cell count equals the most common one."""
    counts: dict[int, int] = {}
    for r in rows:
        counts[len(r)] = counts.get(len(r), 0) + 1
    return max(counts.values()) / len(rows)


def notes_lines(grid: list[list[str]]) -> list[str]:
    """Flatten a notes sheet into text lines, one per row, for the README parser.

    A row with exactly two cells is written as ``key: value`` (the common "label | text" layout) so
    the key/value recogniser sees it; other rows join their cells with a space. Blank rows stay
    blank lines, preserving paragraph breaks.
    """
    lines: list[str] = []
    for row in grid:
        cells = [c.strip() for c in row if c and c.strip()]
        if len(cells) == 2 and len(cells[0]) <= 60:
            sep = " " if cells[0].endswith(":") else ": "
            lines.append(cells[0] + sep + cells[1])
        else:
            lines.append(" ".join(cells))
    return lines
