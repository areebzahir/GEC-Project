"""Read CSV / TSV / TAB / delimited .txt files into a uniform RawTable.

Flow (DESIGN.md section 12): decode a sample -> sniff the dialect -> detect the header on the parsed
grid -> bulk-read every column as strings with Polars using the explicit parameters we found. Polars
is fast but strict about RFC 4180 (Ref [2][13]); when it rejects a messy file we step down a
fallback ladder (drop quoting, then stdlib csv row-by-row) and record which rung we used.
"""

from __future__ import annotations

import csv
import io

import polars as pl

from ..config import PipelineConfig
from ..discovery import DiscoveredFile
from ..issues import Issue, IssueCode, Severity
from ..schema.document import DatasetStructure
from ..text_decoding import decode_bytes
from .base import RawTable
from .classify import looks_like_data_dictionary
from .dialect import sniff_dialect
from .header_detection import clean_headers, collect_header_units, detect_header, is_units_row

_FORMAT_BY_EXT = {".csv": "csv", ".tsv": "tsv", ".tab": "tab", ".txt": "text", ".dat": "dat"}


def read_delimited(file: DiscoveredFile, config: PipelineConfig) -> list[RawTable]:
    """Read one delimited file. Always returns exactly one RawTable (never raises for bad data)."""
    issues: list[Issue] = []
    raw_bytes = file.path.read_bytes()
    decoded = decode_bytes(raw_bytes, config)
    if decoded.rung in {"cp1252", "latin-1"}:
        issues.append(Issue.make(
            IssueCode.ENCODING_FALLBACK, Severity.INFO,
            f"Read {file.relative_path} as {decoded.encoding} (not UTF-8).",
            technical_detail=f"decode rung={decoded.rung}, confidence={decoded.confidence}",
            file=file.relative_path,
        ))

    text = decoded.text
    sample = _sample(text, config.sample_bytes)
    dialect = sniff_dialect(sample, file.extension, config)
    if dialect.confidence < 0.5 and dialect.field_count > 1:
        issues.append(Issue.make(
            IssueCode.DELIMITER_UNCERTAIN, Severity.WARNING,
            f"The column layout of {file.relative_path} was hard to determine confidently.",
            technical_detail=(
                f"best delimiter={dialect.delimiter!r} consistency={dialect.confidence} "
                f"columns={dialect.field_count}"
            ),
            file=file.relative_path,
        ))

    # Parse the sample grid once (after the preamble) to detect the header.
    full_grid = _grid(sample, dialect.delimiter, dialect.quote_char)
    grid = full_grid[dialect.skip_rows :]
    choice = detect_header(grid, config)
    header_row_in_grid = choice.row_index if choice.row_index is not None else 0
    absolute_header_row = dialect.skip_rows + header_row_in_grid  # 0-based within the file

    # A units row ("(kg)", "mg/L", ...) right under the header is metadata, not the first record.
    units_row = None
    if choice.row_index is not None and absolute_header_row + 1 < len(full_grid):
        candidate = full_grid[absolute_header_row + 1]
        if is_units_row(candidate):
            units_row = candidate

    # Read the body with has_header=False so Polars does not silently de-duplicate column names;
    # we take the real header values from the grid and do our own cleaning (DESIGN.md section 12).
    frame, read_issues = _read_frame(
        text, file, dialect, absolute_header_row, choice.row_index is None, config,
        rows_after_header=1 if units_row is not None else 0,
    )
    issues.extend(read_issues)

    width = frame.width
    if choice.row_index is None:
        original_headers = [f"column_{i + 1}" for i in range(width)]
    else:
        row_values = full_grid[absolute_header_row] if absolute_header_row < len(full_grid) else []
        original_headers = _fit_width(row_values, width)

    if choice.row_index is None:
        issues.append(Issue.make(
            IssueCode.HEADER_NOT_FOUND, Severity.WARNING,
            f"Could not identify a header row in {file.relative_path}; generated column names.",
            technical_detail=f"best header score below header_min_score={config.header_min_score}",
            file=file.relative_path,
        ))
    if choice.ambiguous:
        issues.append(Issue.make(
            IssueCode.HEADER_AMBIGUOUS, Severity.WARNING,
            f"Two rows in {file.relative_path} looked equally like the header; chose the earlier one.",
            file=file.relative_path,
        ))

    cleaned, notes = clean_headers(original_headers)
    frame.columns = cleaned
    for kind, detail in notes:
        if kind == "blank":
            issues.append(Issue.make(
                IssueCode.HEADER_BLANK, Severity.WARNING,
                f"A column in {file.relative_path} had no name; named it {detail!r}.",
                file=file.relative_path,
            ))
        else:
            issues.append(Issue.make(
                IssueCode.HEADER_DUPLICATE, Severity.WARNING,
                f"A duplicate column name in {file.relative_path} was renamed ({detail}).",
                file=file.relative_path,
            ))

    structure = DatasetStructure(
        encoding=decoded.encoding,
        delimiter=dialect.delimiter,
        quote_char=dialect.quote_char,
        header_row=None if choice.row_index is None else absolute_header_row + 1,
        header_confidence=choice.confidence,
    )
    # A codebook CSV ("variable,description,unit") documents other files; flag it for the pipeline.
    kind = "dictionary" if looks_like_data_dictionary(cleaned, _sample_rows(frame)) else "data"
    return [RawTable(
        source_name=file.path.stem,
        worksheet_name=None,
        headers=cleaned,
        original_headers=original_headers,
        frame=frame,
        structure=structure,
        declared_format=_FORMAT_BY_EXT.get(file.extension, "delimited"),
        issues=issues,
        kind=kind,
        header_units=collect_header_units(cleaned, original_headers, units_row),
    )]


# Body rows handed to the dictionary detector; enough to judge, cheap on big files.
_DICTIONARY_SAMPLE_ROWS = 50


def _sample_rows(frame: pl.DataFrame) -> list[list[str]]:
    """First body rows as plain strings (None -> "")."""
    return [
        ["" if v is None else str(v) for v in row]
        for row in frame.head(_DICTIONARY_SAMPLE_ROWS).iter_rows()
    ]


def _sample(text: str, limit: int) -> str:
    """First ``limit`` chars, cut back to the last complete line so we never split a row."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    return cut[:nl] if nl > 0 else cut


def _grid(sample: str, delimiter: str, quote_char: str | None) -> list[list[str]]:
    kwargs: dict = {"delimiter": delimiter}
    if quote_char is None:
        kwargs["quoting"] = csv.QUOTE_NONE
    else:
        kwargs["quotechar"] = quote_char
    try:
        return [row for row in csv.reader(io.StringIO(sample), **kwargs)]
    except csv.Error:
        return [line.split(delimiter) for line in sample.splitlines()]


def _read_frame(
    text: str,
    file: DiscoveredFile,
    dialect,
    header_row_0based: int,
    synthesise_header: bool,
    config: PipelineConfig,
    rows_after_header: int = 0,
) -> tuple[pl.DataFrame, list[Issue]]:
    """Read the full file into an all-string DataFrame, stepping down the fallback ladder on error.

    ``rows_after_header`` extra rows (e.g. a units row) are skipped between the header and the body.
    """
    issues: list[Issue] = []
    data = text.encode("utf-8")  # Polars reads UTF-8 bytes; we already decoded correctly
    # Always read with has_header=False and skip through the header row ourselves, so Polars never
    # renames duplicate headers behind our back; the body therefore starts just after the header.
    body_start = dialect.skip_rows if synthesise_header else header_row_0based + 1 + rows_after_header
    common = dict(
        separator=dialect.delimiter,
        has_header=False,
        skip_rows=body_start,
        infer_schema=False,          # every column stays a string; we infer types ourselves
        truncate_ragged_lines=True,  # a short/long row never aborts the read
        null_values=[],              # keep raw strings; missing handling happens in type_inference
    )

    # Rung A: Polars with the sniffed quote char.
    try:
        frame = pl.read_csv(io.BytesIO(data), quote_char=dialect.quote_char, **common)
        return frame, issues
    except Exception as exc_a:  # noqa: BLE001 - Polars raises several concrete types
        # Rung B: disable quote handling (fixes unbalanced quotes; verified in research).
        try:
            frame = pl.read_csv(io.BytesIO(data), quote_char=None, **common)
            issues.append(Issue.make(
                IssueCode.CSV_QUOTE_FALLBACK, Severity.WARNING,
                f"{file.relative_path} had irregular quoting; read it treating quotes as literal text.",
                technical_detail=f"primary read failed: {type(exc_a).__name__}: {str(exc_a).splitlines()[0][:120]}",
                file=file.relative_path,
            ))
            return frame, issues
        except Exception as exc_b:  # noqa: BLE001
            # Rung C: stdlib csv row-by-row, padding/truncating ragged rows to the header width.
            frame = _stdlib_read(text, dialect, header_row_0based, synthesise_header, rows_after_header)
            issues.append(Issue.make(
                IssueCode.CSV_PARSE_FAILED, Severity.WARNING,
                f"{file.relative_path} could not be parsed by the fast reader; used a tolerant fallback.",
                technical_detail=f"{type(exc_b).__name__}: {str(exc_b).splitlines()[0][:120]}",
                file=file.relative_path,
            ))
            return frame, issues


def _fit_width(values: list[str], width: int) -> list[str]:
    """Pad with blanks or truncate so a header row matches the frame's column count."""
    if len(values) < width:
        return values + [""] * (width - len(values))
    return values[:width]


def _stdlib_read(text, dialect, header_row_0based, synthesise_header, rows_after_header=0) -> pl.DataFrame:
    """Tolerant last-resort reader: pads/truncates every row to the header width."""
    kwargs: dict = {"delimiter": dialect.delimiter}
    if dialect.quote_char is None:
        kwargs["quoting"] = csv.QUOTE_NONE
    else:
        kwargs["quotechar"] = dialect.quote_char
    rows = [r for r in csv.reader(io.StringIO(text), **kwargs)]
    rows = rows[header_row_0based:]
    if not rows:
        return pl.DataFrame()
    if synthesise_header:
        width = max(len(r) for r in rows)
        body = rows
    else:
        width = len(rows[0])
        body = rows[1 + rows_after_header :]
    # Positional column names only (the caller overwrites them with the cleaned header), so
    # duplicate names in the file can never collide and drop a column here.
    norm = [(r + [""] * width)[:width] for r in body]
    columns = {f"column_{i + 1}": [r[i] for r in norm] for i in range(width)}
    return pl.DataFrame(columns, schema={f"column_{i + 1}": pl.String for i in range(width)})
