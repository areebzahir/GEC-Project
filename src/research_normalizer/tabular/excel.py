"""Read Excel/ODS workbooks into one RawTable per sheet.

Uses fastexcel (the calamine-backed engine Polars recommends, Ref [1][3][4]). We open each sheet
with ``header_row=None`` to get the raw grid, run the same header detector as the delimited reader,
then read the body as strings. calamine never evaluates formulas or macros, so this is also the safe
choice (DESIGN.md section 23). Hidden sheets are processed but flagged; empty sheets are skipped.
"""

from __future__ import annotations

import polars as pl

from ..config import PipelineConfig
from ..discovery import DiscoveredFile
from ..issues import Issue, IssueCode, Severity
from ..schema.document import DatasetStructure
from .base import RawTable
from .header_detection import clean_headers, detect_header


def read_excel(file: DiscoveredFile, config: PipelineConfig) -> list[RawTable]:
    """Read every data sheet of a workbook. Returns one RawTable per non-empty sheet.

    A workbook that cannot be opened yields a single empty-handed result is avoided: instead we
    return an empty list and the caller records EXCEL_CORRUPT (handled in the pipeline). Here we
    attach the issue to the first readable sheet, or raise it via a sentinel empty table.
    """
    import fastexcel  # local import: keep package import light

    try:
        reader = fastexcel.read_excel(str(file.path))
    except Exception as exc:  # noqa: BLE001 - calamine raises various errors for bad files
        return [_corrupt_sentinel(file, exc)]

    tables: list[RawTable] = []
    for sheet_name in reader.sheet_names:
        table = _read_sheet(reader, sheet_name, file, config)
        if table is not None:
            tables.append(table)

    if not tables:
        # Every sheet was empty; surface that rather than returning nothing silently.
        tables.append(_empty_workbook_sentinel(file))
    return tables


def _read_sheet(reader, sheet_name: str, file: DiscoveredFile, config: PipelineConfig) -> RawTable | None:
    issues: list[Issue] = []

    # header_row=None -> raw grid; dtypes="string" -> every cell as text, like the delimited path.
    try:
        sheet = reader.load_sheet(sheet_name, header_row=None, dtypes="string")
        grid_frame = sheet.to_polars()
    except Exception as exc:  # noqa: BLE001
        issues.append(Issue.make(
            IssueCode.EXCEL_CORRUPT, Severity.ERROR,
            f"Could not read sheet {sheet_name!r} in {file.relative_path}.",
            technical_detail=f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}",
            file=file.relative_path, sheet=sheet_name,
        ))
        return _sheet_with_issues(file, sheet_name, issues)

    visible = getattr(sheet, "visible", "visible")
    if visible != "visible":
        if not config.process_hidden_sheets:
            return None
        issues.append(Issue.make(
            IssueCode.EXCEL_HIDDEN_SHEET, Severity.INFO,
            f"Sheet {sheet_name!r} in {file.relative_path} is {visible}; processed it anyway.",
            file=file.relative_path, sheet=sheet_name,
        ))

    grid = _grid_from_frame(grid_frame)
    # Drop fully-empty trailing/leading rows for detection but keep original indexing meaning.
    if not any(any(c.strip() for c in row) for row in grid):
        issues.append(Issue.make(
            IssueCode.EXCEL_EMPTY_SHEET, Severity.INFO,
            f"Sheet {sheet_name!r} in {file.relative_path} is empty; skipped.",
            file=file.relative_path, sheet=sheet_name,
        ))
        # Return a sentinel (empty frame) so the INFO survives; the pipeline makes no dataset from
        # an empty-frame table but still collects its issues (nothing silently ignored).
        return _sheet_with_issues(file, sheet_name, issues)

    choice = detect_header(grid, config)
    header_idx = choice.row_index if choice.row_index is not None else 0

    if choice.row_index is None:
        width = len(grid[0]) if grid else 0
        original_headers = [f"column_{i + 1}" for i in range(width)]
        body = grid
        issues.append(Issue.make(
            IssueCode.HEADER_NOT_FOUND, Severity.WARNING,
            f"Could not identify a header row in sheet {sheet_name!r}; generated column names.",
            file=file.relative_path, sheet=sheet_name,
        ))
    else:
        original_headers = [c for c in grid[header_idx]]
        body = grid[header_idx + 1 :]
        if choice.ambiguous:
            issues.append(Issue.make(
                IssueCode.HEADER_AMBIGUOUS, Severity.WARNING,
                f"Two rows in sheet {sheet_name!r} looked equally like the header; chose the earlier.",
                file=file.relative_path, sheet=sheet_name,
            ))

    cleaned, notes = clean_headers(original_headers)
    for kind, detail in notes:
        code = IssueCode.HEADER_BLANK if kind == "blank" else IssueCode.HEADER_DUPLICATE
        issues.append(Issue.make(
            code, Severity.WARNING,
            f"Header issue in sheet {sheet_name!r}: {detail}.",
            file=file.relative_path, sheet=sheet_name,
        ))

    frame = _body_frame(body, cleaned)
    structure = DatasetStructure(
        encoding="binary-excel",
        delimiter=None,
        quote_char=None,
        header_row=None if choice.row_index is None else header_idx + 1,
        header_confidence=choice.confidence,
    )
    return RawTable(
        source_name=f"{file.path.stem} - {sheet_name}",
        worksheet_name=sheet_name,
        headers=cleaned,
        original_headers=original_headers,
        frame=frame,
        structure=structure,
        declared_format="excel",
        issues=issues,
    )


def _grid_from_frame(frame: pl.DataFrame) -> list[list[str]]:
    """Turn an all-string Polars grid into a list-of-rows of plain strings (None -> "")."""
    rows: list[list[str]] = []
    for row in frame.iter_rows():
        rows.append(["" if cell is None else str(cell) for cell in row])
    return rows


def _body_frame(body: list[list[str]], columns: list[str]) -> pl.DataFrame:
    """Build an all-string DataFrame from body rows, padded/truncated to the header width."""
    width = len(columns)
    if width == 0:
        return pl.DataFrame()
    norm = [(r + [""] * width)[:width] for r in body]
    data = {columns[i]: [r[i] for r in norm] for i in range(width)}
    return pl.DataFrame(data, schema={c: pl.String for c in columns})


def _sheet_with_issues(file: DiscoveredFile, sheet_name: str, issues: list[Issue]) -> RawTable:
    """A placeholder table carrying only issues, for a sheet that failed to read."""
    return RawTable(
        source_name=f"{file.path.stem} - {sheet_name}",
        worksheet_name=sheet_name,
        headers=[],
        original_headers=[],
        frame=pl.DataFrame(),
        structure=DatasetStructure(encoding="binary-excel", header_row=None, header_confidence=0.0),
        declared_format="excel",
        issues=issues,
    )


def _corrupt_sentinel(file: DiscoveredFile, exc: Exception) -> RawTable:
    issue = Issue.make(
        IssueCode.EXCEL_CORRUPT, Severity.ERROR,
        f"Could not open the Excel file {file.relative_path}.",
        technical_detail=f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}",
        file=file.relative_path,
    )
    return _sheet_with_issues(file, "", [issue])


def _empty_workbook_sentinel(file: DiscoveredFile) -> RawTable:
    issue = Issue.make(
        IssueCode.EXCEL_EMPTY_SHEET, Severity.WARNING,
        f"The Excel file {file.relative_path} has no non-empty sheets.",
        file=file.relative_path,
    )
    return _sheet_with_issues(file, "", [issue])
