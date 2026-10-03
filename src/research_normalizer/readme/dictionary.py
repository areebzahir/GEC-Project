"""Turn a data-dictionary table (a codebook CSV or an Excel sheet) into variable definitions.

Many repositories document variables in a table (variable | description | units | values) rather
than in prose. This module converts such a table into the same ``ParsedReadme`` structure a prose
README produces, so the rest of the pipeline treats both sources identically.
"""

from __future__ import annotations

from .parser import ParsedReadme, VariableGroup
from .project_fields import ProjectFields
from .variable_layouts import classify_header, definition_from_cells


def parse_dictionary(rows: list[list[str]], file_name: str, sheet: str | None = None) -> ParsedReadme:
    """Parse a dictionary table (``rows[0]`` = header) into a ParsedReadme.

    One VariableGroup per distinct value of a "file"/"table" column (or a single group if the table
    has no such column). Line numbers are 1-based table rows (header = row 1) for provenance.
    """
    parsed = ParsedReadme(
        file_name=file_name, doc_format="dictionary", encoding="n/a", sections=[],
        project=ProjectFields(), variable_groups=[], abbreviations={}, referenced_files=[],
        source_kind="dictionary",
    )
    if len(rows) < 2:
        return parsed

    roles = classify_header(rows[0])
    if "name" not in roles:
        return parsed

    groups: dict[tuple[str | None, str | None], VariableGroup] = {}
    for row_number, cells in enumerate(rows[1:], start=2):
        definition = definition_from_cells(cells, roles, row_number, "dictionary")
        if definition is None:
            continue
        file_hint = _cell(cells, roles.get("file")) or None
        sheet_hint = _cell(cells, roles.get("sheet")) or None
        key = (file_hint, sheet_hint)
        if key not in groups:
            groups[key] = VariableGroup(
                file_hint=file_hint, variables=[], sheet_hint=sheet_hint,
                section_title=f"data dictionary ({sheet})" if sheet else "data dictionary",
                readme_file=file_name, section_lines=(1, len(rows)),
            )
        definition.position = len(groups[key].variables) + 1
        groups[key].variables.append(definition)

    parsed.variable_groups = list(groups.values())
    parsed.referenced_files = [f for f, _ in groups if f]
    return parsed


def _cell(cells: list[str], index: int | None) -> str:
    return cells[index].strip() if index is not None and index < len(cells) else ""
