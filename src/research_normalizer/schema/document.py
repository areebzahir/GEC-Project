"""The canonical standardized JSON document.

These Pydantic models ARE the output schema (DESIGN.md section 15). Field names follow Frictionless
Table Schema for variables (Ref [14]) and DataCite for project metadata (Ref [17]), so the format is
interoperable and defensible. The committed JSON Schema in ``schemas/`` is generated from these
models, so the two can never drift (there is a test for it).

Layout, top to bottom: project -> datasets -> variables, mirroring the competition's Project/Dataset
tree. Cross-dataset facts live in ``relationships``; everything that went wrong lives in ``issues``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from ..issues import Issue
from .provenance import Source


# --------------------------------------------------------------------------- shared small types
class ColumnType(StrEnum):
    """Logical column types, a readable subset of Frictionless Table Schema (Ref [14])."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"


class ColumnRole(StrEnum):
    """A coarse hint about how a column is used, for display and matching context."""

    IDENTIFIER = "identifier"
    CATEGORICAL = "categorical"
    MEASURE = "measure"
    TEMPORAL = "temporal"
    TEXT = "text"


class MatchStatus(StrEnum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"


class ValueLabel(BaseModel):
    """A categorical code and its human-readable meaning, e.g. {"code": "1", "label": "Male"}."""

    code: str
    label: str


class Person(BaseModel):
    """An author/investigator/contact parsed from the README (DataCite 'creator', Ref [17])."""

    name: str
    role: str | None = None
    orcid: str | None = None
    affiliation: str | None = None
    email: str | None = None


class DateRange(BaseModel):
    """A collection period. ``text`` always preserves the original string."""

    text: str
    start: str | None = None  # ISO 8601 when parseable
    end: str | None = None


class KeyValue(BaseModel):
    """A README key/value we kept but did not map to a canonical field (nothing is dropped)."""

    label: str
    value: str
    source: Source | None = None


class MethodologyNote(BaseModel):
    heading: str | None = None
    text: str


# --------------------------------------------------------------------------- project
class Project(BaseModel):
    """Repository-level metadata, mostly from the README(s)."""

    title: str | None = None
    description: str | None = None
    creators: list[Person] = Field(default_factory=list)
    contacts: list[Person] = Field(default_factory=list)
    identifier: str | None = None
    collection_period: DateRange | None = None
    geographic_location: str | None = None
    funding: list[str] = Field(default_factory=list)
    license: str | None = None
    related_publications: list[str] = Field(default_factory=list)
    related_datasets: list[str] = Field(default_factory=list)
    citation: str | None = None
    keywords: list[str] = Field(default_factory=list)
    methodology: list[MethodologyNote] = Field(default_factory=list)
    additional_fields: list[KeyValue] = Field(default_factory=list)
    sources: dict[str, Source] = Field(default_factory=dict)
    """Maps a field name (e.g. 'title') to where it came from."""


# --------------------------------------------------------------------------- documents
class DocumentSection(BaseModel):
    title: str | None = None
    lines: tuple[int, int]
    text: str | None = None
    """The section's text as parsed (its blocks joined by newlines), so nothing is lost."""


class DocumentInfo(BaseModel):
    """A documentation file we parsed and which datasets it describes."""

    file: str
    format: str
    encoding: str
    describes: list[str] = Field(default_factory=list)
    """Dataset files this document was bound to; a worksheet is written as ``file#sheet``."""
    sections: list[DocumentSection] = Field(default_factory=list)


# --------------------------------------------------------------------------- variables
class VariableMatch(BaseModel):
    """How a dataset column was linked to a documented variable (DESIGN.md section 14)."""

    status: MatchStatus
    documented_name: str | None = None
    confidence: float = 0.0
    method: str | None = None
    evidence: list[str] = Field(default_factory=list)
    alternatives: list["MatchAlternative"] = Field(default_factory=list)
    warning: str | None = None


class MatchAlternative(BaseModel):
    documented_name: str
    confidence: float


class ColumnStatistics(BaseModel):
    """One-pass profile of a column (DESIGN.md section 13)."""

    count: int
    missing: int
    distinct: int
    min: float | str | None = None
    max: float | str | None = None
    mean: float | None = None
    std: float | None = None
    top_values: list[str] = Field(default_factory=list)


class Variable(BaseModel):
    """A single dataset column, enriched with documentation and provenance."""

    name: str
    original_name: str
    position: int
    label: str | None = None
    description: str | None = None
    unit: str | None = None
    type: ColumnType = ColumnType.STRING
    format: str = "default"
    role: ColumnRole = ColumnRole.TEXT
    missing_values: list[str] = Field(default_factory=list)
    value_labels: list[ValueLabel] = Field(default_factory=list)
    notes: str | None = None
    statistics: ColumnStatistics | None = None
    match: VariableMatch
    sources: dict[str, Source] = Field(default_factory=dict)


# --------------------------------------------------------------------------- datasets
class DatasetStructure(BaseModel):
    """How the physical table was read (DESIGN.md sections 10, 12)."""

    encoding: str
    delimiter: str | None = None
    quote_char: str | None = None
    header_row: int | None = None  # 1-based row number of the chosen header, None if synthesised
    header_confidence: float = 1.0


class DeclaredCounts(BaseModel):
    """Counts the README claimed, kept so we can cross-check them (DESIGN.md section 13)."""

    variable_count: int | None = None
    row_count: int | None = None


class UnmatchedVariable(BaseModel):
    """A variable the README documents but which no column matched."""

    name: str
    description: str | None = None
    lines: tuple[int, int] | None = None
    file: str | None = None
    """The documentation file that describes the variable."""


class Dataset(BaseModel):
    """One data table (one CSV/TAB file, or one Excel sheet)."""

    id: str
    title: str | None = None
    file: str
    documented_as: str | None = None
    """The filename the README used, if different from the actual file (e.g. .csv vs .tab)."""
    worksheet_name: str | None = None
    description: str | None = None
    format: str
    structure: DatasetStructure
    row_count: int
    column_count: int
    declared: DeclaredCounts = Field(default_factory=DeclaredCounts)
    headers: list[str] = Field(default_factory=list)
    last_updated: str | None = None
    variables: list[Variable] = Field(default_factory=list)
    unmatched_documented_variables: list[UnmatchedVariable] = Field(default_factory=list)
    records: list[dict] = Field(default_factory=list)
    records_included: int = 0
    records_truncated: bool = False


# --------------------------------------------------------------------------- relationships & files
class Relationship(BaseModel):
    """A link between datasets or a documented external link.

    * ``shared_variables``: same-named columns with compatible types and overlapping values.
    * ``documented_join``: the README states that files are related (its text is in ``evidence``).
    * ``cross_file_reference``: a README section documents a variable that lives in another file.
    * ``documented_link``: an external URL the README points to.
    """

    type: Literal["shared_variables", "documented_link", "documented_join", "cross_file_reference"]
    datasets: list[str] = Field(default_factory=list)
    variables: list[str] = Field(default_factory=list)
    from_: str | None = Field(default=None, alias="from")
    target: str | None = None
    confidence: float = 1.0
    source: Source | None = None
    """Where the relationship was stated, for documented ones."""
    evidence: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class FileRecord(BaseModel):
    """One file found in the repository and what became of it."""

    path: str
    role: Literal["tabular", "documentation", "other"]
    format: str
    size_bytes: int
    sha256: str
    status: Literal["processed", "skipped", "failed"]


# --------------------------------------------------------------------------- top level
class RepositoryInfo(BaseModel):
    name: str
    file_count: int


class Summary(BaseModel):
    datasets: int = 0
    variables: int = 0
    matched: int = 0
    undocumented: int = 0
    warnings: int = 0
    errors: int = 0


class Processing(BaseModel):
    tool_version: str
    schema_version: str
    started_at: str | None = None
    duration_ms: int | None = None
    stage_durations_ms: dict[str, int] = Field(default_factory=dict)


class RepositoryDocument(BaseModel):
    """The complete standardized output for one repository."""

    schema_version: str
    repository: RepositoryInfo
    project: Project
    documents: list[DocumentInfo] = Field(default_factory=list)
    datasets: list[Dataset] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    files: list[FileRecord] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    summary: Summary = Field(default_factory=Summary)
    processing: Processing

    model_config = {"json_schema_extra": {"$id": "https://gec2026/research_repository.schema.json"}}


# Resolve the forward reference between VariableMatch and MatchAlternative.
VariableMatch.model_rebuild()
