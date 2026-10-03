"""The uniform table representation and the reader registry.

Every format reader produces a :class:`RawTable`: a Polars DataFrame of *strings* (so we, not the
reader, decide types and missing values), the final header, a small raw grid used for header
detection, the detected structure, and any issues raised while reading. ``read_table`` dispatches to
the right reader by file role/extension (DESIGN.md section 12).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from ..config import EXCEL_EXTENSIONS, PipelineConfig
from ..discovery import DiscoveredFile
from ..issues import Issue
from ..schema.document import DatasetStructure


@dataclass(slots=True)
class RawTable:
    """One table read from disk, before type inference.

    ``frame`` holds every column as ``pl.String`` so missing-value handling and type inference stay
    under our control (DESIGN.md section 13). ``headers`` is the cleaned, de-duplicated header;
    ``original_headers`` preserves exactly what was in the file.
    """

    source_name: str                       # dataset title candidate (file stem or sheet name)
    worksheet_name: str | None
    headers: list[str]
    original_headers: list[str]
    frame: pl.DataFrame
    structure: DatasetStructure
    declared_format: str                    # csv | tsv | tab | excel | ...
    issues: list[Issue] = field(default_factory=list)


# A table reader turns one discovered file into one-or-more RawTables (Excel yields one per sheet).
# Implemented as plain functions registered below rather than classes, to keep the surface small.
def read_table(
    file: DiscoveredFile,
    config: PipelineConfig,
) -> list[RawTable]:
    """Dispatch a tabular file to the correct reader based on its extension.

    Imports of the concrete readers are local so that importing this module does not pull in Polars
    or fastexcel until a table is actually read.
    """
    if file.extension in EXCEL_EXTENSIONS:
        from .excel import read_excel

        return read_excel(file, config)

    from .delimited import read_delimited

    return read_delimited(file, config)
