"""Parse one documentation file into a structured, scoped result.

Ties together loaders -> segmenter -> project fields -> variable layouts -> abbreviations
(DESIGN.md section 11). Variable definitions are grouped by the data file they describe: a section
whose heading or a "File List" entry names a file binds its variables to that file; everything else
is repository-wide. The actual file<->README linking (matching ``X.csv`` to ``X.tab``) happens in
``linking.py`` using the filenames captured here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import PipelineConfig
from ..issues import Issue
from .abbreviations import extract_abbreviations
from .loaders import load_document
from .absence import is_exclusion_title
from .missing_codes import codes_from_key_value, codes_from_text
from .project_fields import ProjectFields, extract_project_fields
from .relationships import find_relationships
from .scope import ScopeHint, hint_from_heading, hint_from_lead_line
from .segmenter import Block, BlockType, Section, segment
from .variable_layouts import (
    VariableDefinition,
    find_declared_counts,
    line_coverage,
    mark_absent,
    recognise_variables,
)

# A filename mentioned in a heading like "DATA-SPECIFIC INFORMATION FOR: survey.csv" or in a File
# List entry. Captures common tabular/documentation extensions.
_FILENAME_RE = re.compile(r"([\w\-.]+\.(?:csv|tsv|tab|txt|xlsx|xls|xlsm|ods|dat))", re.IGNORECASE)


@dataclass(slots=True)
class VariableGroup:
    """Variable definitions bound to a particular data file (or global if ``file_hint`` is None)."""

    file_hint: str | None                 # filename as written in the README, e.g. "survey.csv"
    variables: list[VariableDefinition]
    declared_variable_count: int | None = None
    declared_row_count: int | None = None
    missing_codes: list[str] = field(default_factory=list)
    section_title: str | None = None
    readme_file: str | None = None        # the documentation file these definitions came from
    sheet_hint: str | None = None         # worksheet the README names for these variables, if any
    section_lines: tuple[int, int] | None = None  # line span of the section the group came from


@dataclass(slots=True)
class DocumentedRelationship:
    """A relationship the README states explicitly, e.g. "a.csv and b.csv join on site_id"."""

    files: list[str]                      # filenames as written in the README
    variables: list[str]                  # key/linking variable names, if stated
    text: str                             # the README sentence, kept verbatim for provenance
    line_start: int = 0
    line_end: int = 0


@dataclass(slots=True)
class ParsedReadme:
    """Everything extracted from one documentation file."""

    file_name: str
    doc_format: str
    encoding: str
    sections: list[Section]
    project: ProjectFields
    variable_groups: list[VariableGroup]
    abbreviations: dict[str, str]
    referenced_files: list[str]           # all filenames the README mentions
    issues: list[Issue] = field(default_factory=list)
    relationships: list[DocumentedRelationship] = field(default_factory=list)
    global_missing_codes: list[str] = field(default_factory=list)  # repository-wide missing codes
    source_kind: str = "readme"           # "readme" | "dictionary" (a data-dictionary table/sheet)


def parse_readme(
    path: Path,
    extension: str,
    config: PipelineConfig,
    documented_columns: frozenset[str] = frozenset(),
) -> ParsedReadme:
    """Parse a documentation file end to end."""
    doc = load_document(path, extension, config)
    parsed = parse_lines(doc.lines, path.name, documented_columns,
                         doc_format=doc.doc_format, encoding=doc.encoding)
    parsed.issues.extend(doc.issues)
    return parsed


def parse_lines(
    lines: list[str],
    file_name: str,
    documented_columns: frozenset[str] = frozenset(),
    *,
    doc_format: str = "text",
    encoding: str = "n/a",
) -> ParsedReadme:
    """Parse already-loaded documentation lines (a README file, or a notes sheet in a workbook)."""
    sections = segment(lines)
    full_text = "\n".join(lines)

    project = extract_project_fields(sections, file_name, full_text)
    abbreviations = extract_abbreviations(full_text)
    referenced = _unique(_FILENAME_RE.findall(full_text))

    groups, global_missing = _extract_variable_groups(sections, documented_columns)
    for group in groups:
        group.readme_file = file_name

    relationships = [
        DocumentedRelationship(files=s.files, variables=s.variables, text=s.text,
                               line_start=s.line_start, line_end=s.line_end)
        for s in find_relationships(sections)
    ]

    return ParsedReadme(
        file_name=file_name,
        doc_format=doc_format,
        encoding=encoding,
        sections=sections,
        project=project,
        variable_groups=groups,
        abbreviations=abbreviations,
        referenced_files=referenced,
        relationships=relationships,
        global_missing_codes=global_missing,
    )


# Section titles/keys that mean "this is where variables are described".
_VARIABLE_SECTION_CUES = ("variable", "data-specific", "data specific", "column", "field list",
                          "codebook", "data dictionary", "dictionary")
# Section titles that are definitely NOT variable lists (people, prose, admin).
_NON_VARIABLE_CUES = ("investigator", "contact", "author", "general information", "methodolog",
                      "sharing", "access", "citation", "overview", "funding")
# Keys that carry a data-specific filename, e.g. "DATA-SPECIFIC INFORMATION FOR: survey.csv".
_FILE_CONTEXT_KEYS = ("data-specific information for", "data specific information for",
                      "data-specific information", "complete dataset", "filename", "file name")


def _extract_variable_groups(
    sections: list[Section],
    documented_columns: frozenset[str],
) -> tuple[list[VariableGroup], list[str]]:
    """Find variable definitions, binding them to the data file named in the surrounding context.

    We walk the sections in order while tracking a running "data context" (the filename, declared
    counts and missing codes most recently seen). READMEs commonly state these just *before* the
    variable list and in a different segmented section, so a running context links them correctly
    (DESIGN.md section 11).
    """
    groups: list[VariableGroup] = []
    global_missing: list[str] = []
    ctx_file: str | None = None
    ctx_vars: int | None = None
    ctx_rows: int | None = None
    ctx_missing: list[str] = []

    for section in sections:
        # Which file do this section's variables belong to?
        #  - If the section HEADING names a file ("DATA-SPECIFIC INFORMATION FOR: X.csv"), use it.
        #  - Otherwise use whatever filename was in effect before this section. A filename that only
        #    appears in a trailing block announces the NEXT dataset, so it must not re-bind this one.
        heading_file = _filename_in_title(section)
        file_for_this_section = heading_file or ctx_file

        found_file = _file_in_section(section)
        if found_file:
            ctx_file = found_file
        var_count, row_count = find_declared_counts(section)
        if var_count is not None:
            ctx_vars = var_count
        if row_count is not None:
            ctx_rows = row_count
        missing = _missing_in_section(section)
        if missing:
            ctx_missing = missing
            if file_for_this_section is None and not _is_variable_section(section, documented_columns):
                global_missing = missing

        if not _is_variable_section(section, documented_columns):
            continue
        variables = recognise_variables(section, documented_columns)
        variables = [v for v in variables if _looks_like_variable(v.name)]
        if not variables:
            continue
        mark_absent(variables, section)

        groups.append(VariableGroup(
            file_hint=file_for_this_section,
            variables=variables,
            declared_variable_count=ctx_vars,
            declared_row_count=ctx_rows,
            missing_codes=list(ctx_missing),
            section_title=section.title,
            section_lines=(section.line_start, section.line_end),
        ))
    return groups, global_missing


def _missing_in_section(section: Section) -> list[str]:
    """Missing-value codes stated in a section, from key/values or prose sentences."""
    for block in section.blocks:
        if block.type is BlockType.KEY_VALUE and block.key:
            codes = codes_from_key_value(block.key, block.value or "")
            if codes is not None:
                return codes
        elif block.type is BlockType.TEXT:
            codes = codes_from_text(block.text)
            if codes:
                return codes
    return []


def _is_variable_section(section: Section, documented_columns: frozenset[str]) -> bool:
    """Decide whether a section describes variables (vs. people, methods, admin prose)."""
    title = (section.title or "").lower()
    if any(cue in title for cue in _NON_VARIABLE_CUES) and not any(
        cue in title for cue in _VARIABLE_SECTION_CUES
    ):
        return False
    if any(cue in title for cue in _VARIABLE_SECTION_CUES):
        return True
    # No explicit title cue: accept only if several lines start with a known dataset column.
    if documented_columns:
        from ..matching.normalize import normalize_name

        norm_cols = {normalize_name(c) for c in documented_columns}
        hits = 0
        for block in section.blocks:
            first = re.split(r"[\s,:=\-\t]", block.text.strip(), maxsplit=1)[0]
            if normalize_name(first) in norm_cols:
                hits += 1
        return hits >= 3
    return False


def _looks_like_variable(name: str) -> bool:
    """Reject obvious non-variables (people names, sentence fragments) that slipped through."""
    name = name.strip()
    if not name:
        return False
    # A variable name is short; a multi-word Title Case phrase is usually a person or sentence.
    words = name.split()
    if len(words) > 5:
        return False
    return True


def _filename_in_title(section: Section) -> str | None:
    """A data filename named in the section's own heading, e.g. 'DATA-SPECIFIC INFORMATION FOR: x.csv'."""
    if section.title:
        m = _FILENAME_RE.search(section.title)
        if m:
            return m.group(1)
    return None


def _file_in_section(section: Section) -> str | None:
    """Find a data filename named in a section title or in a file-context key/value."""
    if section.title:
        m = _FILENAME_RE.search(section.title)
        if m:
            return m.group(1)
    from .segmenter import BlockType

    for block in section.blocks:
        if block.type is BlockType.KEY_VALUE and block.key:
            key = block.key.strip().lower()
            if any(k in key for k in _FILE_CONTEXT_KEYS):
                m = _FILENAME_RE.search(block.value or "")
                if m:
                    return m.group(1)
    return None


def _unique(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)
