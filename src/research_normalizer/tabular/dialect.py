"""Detect the dialect of a delimited file: delimiter, quote char, and preamble rows.

CSV files are not self-describing, so we cannot assume a delimiter (a semicolon file read as CSV
comes back as one column). We adopt the published consistency idea behind DuckDB's sniffer and
CleverCSV (Ref [10][11][25]): parse a sample under each candidate dialect and keep the one that
produces the most columns with the most consistent row lengths. This is deterministic and ~1 screen
of code, and it feeds explicit parameters to Polars rather than hoping Polars guesses right.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from ..config import CANDIDATE_DELIMITERS, CANDIDATE_QUOTE_CHARS, PipelineConfig

# A run of at least this many equal-width rows marks the start of the "real" table; rows before it
# are treated as preamble (notes/titles) and excluded from scoring (DESIGN.md section 12 step 2).
_MIN_CONSISTENT_RUN = 3


@dataclass(frozen=True, slots=True)
class Dialect:
    """A sniffed dialect plus the confidence and preamble offset we derived with it."""

    delimiter: str
    quote_char: str | None
    skip_rows: int        # number of preamble rows before the header
    confidence: float     # 0..1, how consistent the sample was under this dialect
    field_count: int      # modal number of columns


def _parse_rows(sample: str, delimiter: str, quote_char: str | None) -> list[list[str]]:
    """Parse the sample with the stdlib csv reader under one candidate dialect."""
    kwargs: dict = {"delimiter": delimiter}
    if quote_char is None:
        # QUOTE_NONE means quotes are literal; escapechar None keeps it simple.
        kwargs["quoting"] = csv.QUOTE_NONE
    else:
        kwargs["quotechar"] = quote_char
    try:
        return [row for row in csv.reader(io.StringIO(sample), **kwargs)]
    except csv.Error:
        return []


def _score(rows: list[list[str]]) -> tuple[float, int, int]:
    """Score a parse: (consistency, modal_field_count, preamble_rows).

    Consistency is measured over *all* non-empty rows, not just the rows after the preamble. This is
    deliberate: a dialect that needs to discard the header row to look consistent (e.g. reading a
    ``;``-delimited file as CSV, where only the data rows happen to split into equal pieces) should
    score below a dialect under which every row — header included — is already uniform. The preamble
    offset is still returned, but only to tell the header detector where the real table starts.
    """
    widths = [len(r) for r in rows if r]
    if not widths:
        return (0.0, 0, 0)

    # Modal width, preferring wider shapes on ties (more columns = more structure recovered).
    counts: dict[int, int] = {}
    for w in widths:
        counts[w] = counts.get(w, 0) + 1
    modal = max(counts, key=lambda w: (counts[w], w))
    if modal <= 1:
        return (0.0, modal, 0)

    # Consistency over every non-empty row.
    consistency = sum(1 for w in widths if w == modal) / len(widths)

    # Preamble = index of the first row in a run of >= _MIN_CONSISTENT_RUN modal-width rows.
    preamble = 0
    run = 0
    non_empty = [r for r in rows if r]
    for i, r in enumerate(non_empty):
        if len(r) == modal:
            run += 1
            if run >= _MIN_CONSISTENT_RUN:
                preamble = i - run + 1
                break
        else:
            run = 0
    return (consistency, modal, preamble)


def sniff_dialect(sample: str, extension: str, config: PipelineConfig) -> Dialect:
    """Return the best dialect for ``sample``.

    ``extension`` only breaks ties: ``.tab``/``.tsv`` nudge toward tab, ``.csv`` toward comma. It is
    never allowed to override a clearly better-scoring delimiter (DESIGN.md section 12: dialect
    overrides extension).
    """
    extension_prior = {".tsv": "\t", ".tab": "\t", ".csv": ",", ".dat": "\t"}.get(extension)

    best: Dialect | None = None
    best_score = -1.0
    for delimiter in CANDIDATE_DELIMITERS:
        for quote_char in CANDIDATE_QUOTE_CHARS:
            rows = _parse_rows(sample, delimiter, quote_char)
            consistency, modal, preamble = _score(rows)
            if modal <= 1:
                continue
            # Composite score: consistency dominates, modal width breaks near-ties, and the
            # extension prior adds only a tiny nudge so it can never override a clearly better fit.
            score = consistency + 0.001 * modal + (0.01 if delimiter == extension_prior else 0.0)
            if score > best_score:
                best_score = score
                best = Dialect(delimiter, quote_char, preamble, round(consistency, 3), modal)

    if best is None:
        # No candidate produced multiple columns: fall back to a single-column read with the prior.
        return Dialect(extension_prior or ",", '"', 0, 0.0, 1)
    return best
