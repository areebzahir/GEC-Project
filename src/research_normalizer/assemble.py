"""Assemble stage results into the canonical document.

Given a parsed table, its matched variables and the README definitions bound to it, build a
``Dataset`` (variables, typed records, statistics, provenance). Also builds the ``Project`` from the
parsed READMEs and detects cross-dataset ``relationships`` (shared columns) (DESIGN.md sections 14, 15).
Pure functions only: inputs in, models out, issues returned alongside.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import polars as pl

from .config import PipelineConfig
from .issues import Issue, IssueCode, Severity
from .matching.assign import ColumnContext, match_variables
from .matching.normalize import normalize_name
from .readme.parser import ParsedReadme, VariableGroup
from .schema.document import (
    ColumnType,
    DeclaredCounts,
    Dataset,
    Project,
    Relationship,
    UnmatchedVariable,
    Variable,
)
from .schema.provenance import Source
from .tabular.base import RawTable
from .tabular.type_inference import infer_column_type
from .tabular.profiling import profile_column

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Polars dtypes that serialize cleanly to JSON per inferred column type.
_CAST_TARGET = {
    ColumnType.INTEGER: pl.Int64,
    ColumnType.NUMBER: pl.Float64,
    ColumnType.BOOLEAN: pl.Boolean,
}


def slugify(text: str) -> str:
    """A deterministic lowercase slug used for dataset ids."""
    return _SLUG_RE.sub("_", text.strip().lower()).strip("_") or "dataset"


def build_dataset(
    table: RawTable,
    relative_path: str,
    groups: list[VariableGroup],
    aliases: dict[str, str],
    config: PipelineConfig,
) -> tuple[Dataset, list[Issue]]:
    """Build one canonical ``Dataset`` from a parsed table and its documentation."""
    issues: list[Issue] = []
    frame = table.frame

    # Merge all variable definitions and declared metadata bound to this file.
    definitions = [d for g in groups for d in g.variables]
    extra_missing = {code for g in groups for code in g.missing_codes}
    all_missing = set(config.missing_value_tokens) | extra_missing
    declared = _merge_declared(groups)
    documented_as = _documented_as(groups)

    # Type, profile and build each column.
    contexts: list[ColumnContext] = []
    typed: dict[str, ColumnType] = {}
    profiles = {}
    for position, name in enumerate(frame.columns):
        series = frame.get_column(name)
        type_result = infer_column_type(series, name, relative_path, extra_missing, config)
        issues.extend(type_result.issues)
        stats = profile_column(series, type_result.type, all_missing)
        typed[name] = type_result.type
        profiles[name] = (type_result, stats)
        distinct_values = frozenset(stats.top_values) if type_result.type in (
            ColumnType.STRING, ColumnType.BOOLEAN
        ) else frozenset()
        contexts.append(ColumnContext(
            name=name, position=position, column_type=type_result.type,
            distinct_values=distinct_values, distinct_count=stats.distinct,
        ))

    outcome = match_variables(contexts, definitions, aliases, config)
    def_by_name = {d.name: d for d in definitions}
    source_file = _source_file(groups)

    variables: list[Variable] = []
    for position, name in enumerate(frame.columns):
        type_result, stats = profiles[name]
        match = outcome.matches[name]
        defn = def_by_name.get(match.documented_name) if match.documented_name else None

        sources: dict[str, Source] = {}
        if defn is not None and source_file is not None:
            sources["description"] = Source(
                file=source_file, section=None,
                lines=(defn.line_start, defn.line_end), method=defn.method,
            )

        variables.append(Variable(
            name=name,
            original_name=table.original_headers[position] if position < len(table.original_headers) else name,
            position=position,
            label=defn.description if defn else None,
            description=defn.description if defn else None,
            unit=defn.unit if defn else None,
            type=type_result.type,
            format=type_result.format,
            role=type_result.role,
            missing_values=type_result.missing_values,
            value_labels=defn.value_labels if defn else [],
            notes=defn.notes if defn else None,
            statistics=stats,
            match=match,
            sources=sources,
        ))
        issues.extend(_variable_issues(name, match, defn, type_result, relative_path, config))

    # Documented-but-absent variables.
    for u in outcome.unmatched_documented:
        issues.append(Issue.make(
            IssueCode.DOCUMENTED_VARIABLE_NOT_IN_DATA, Severity.WARNING,
            f"The README describes a variable {u.name!r} that does not appear in {relative_path}.",
            technical_detail="No column scored at or above the review threshold.",
            file=source_file, lines=u.lines,
            suggestion="Check whether the column was renamed or removed from the data file.",
        ))

    # Declared-count cross-checks (DESIGN.md section 13).
    issues.extend(_count_checks(declared, len(frame.columns), frame.height, relative_path, source_file))

    records, included, truncated = _records(frame, typed, all_missing, config)

    dataset = Dataset(
        id=slugify(table.source_name),
        title=_dataset_title(groups) or table.source_name,
        file=relative_path,
        documented_as=documented_as,
        worksheet_name=table.worksheet_name,
        description=None,
        format=table.declared_format,
        structure=table.structure,
        row_count=frame.height,
        column_count=frame.width,
        declared=declared,
        headers=list(frame.columns),
        last_updated=None,
        variables=variables,
        unmatched_documented_variables=outcome.unmatched_documented,
        records=records,
        records_included=included,
        records_truncated=truncated,
    )
    return dataset, issues


def _records(
    frame: pl.DataFrame,
    typed: dict[str, ColumnType],
    missing: set[str],
    config: PipelineConfig,
) -> tuple[list[dict], int, bool]:
    """Produce typed JSON records: missing codes -> null, each column cast to its inferred type.

    The cast is non-strict and only applied where the whole column was accepted as that type, so a
    value is never coerced lossily (the type-inference guarantee, DESIGN.md section 13).
    """
    limit = config.max_records_per_dataset
    truncated = limit is not None and frame.height > limit
    view = frame.head(limit) if limit is not None else frame

    # Null out missing tokens, then cast typed columns in a single vectorized pass.
    exprs = []
    for name in view.columns:
        col = pl.col(name).str.strip_chars()
        col = pl.when(col.is_in(list(missing)) | (col == "")).then(None).otherwise(col)
        target = _CAST_TARGET.get(typed[name])
        if target is not None:
            col = col.cast(target, strict=False)
        exprs.append(col.alias(name))
    cast_frame = view.select(exprs)
    return cast_frame.to_dicts(), view.height, truncated


def _variable_issues(name, match, defn, type_result, relative_path, config) -> list[Issue]:
    issues: list[Issue] = []
    if match.status.value == "unmatched" and defn is None:
        issues.append(Issue.make(
            IssueCode.VARIABLE_UNDOCUMENTED, Severity.WARNING,
            f"Column {name!r} in {relative_path} has no description in the README.",
            file=relative_path, column=name,
            suggestion="Add a definition for this variable to the README.",
        ))
    elif match.status.value == "ambiguous":
        issues.append(Issue.make(
            IssueCode.MATCH_AMBIGUOUS, Severity.WARNING,
            f"Column {name!r} matched {match.documented_name!r} but another variable was almost as close.",
            technical_detail=f"confidence={match.confidence}, alternatives={[a.documented_name for a in match.alternatives]}",
            file=relative_path, column=name,
        ))
    elif match.documented_name and match.confidence < config.match_accept_threshold:
        issues.append(Issue.make(
            IssueCode.MATCH_LOW_CONFIDENCE, Severity.WARNING,
            f"Column {name!r} was matched to {match.documented_name!r} with low confidence.",
            technical_detail=f"confidence={match.confidence}, method={match.method}",
            file=relative_path, column=name,
        ))
    return issues


def _count_checks(declared: DeclaredCounts, columns: int, rows: int, relative_path, source_file) -> list[Issue]:
    issues: list[Issue] = []
    if declared.variable_count is not None and declared.variable_count != columns:
        issues.append(Issue.make(
            IssueCode.DECLARED_COUNT_MISMATCH, Severity.WARNING,
            f"The README says {relative_path} has {declared.variable_count} variables, but {columns} columns were found.",
            file=source_file,
        ))
    if declared.row_count is not None and declared.row_count != rows:
        issues.append(Issue.make(
            IssueCode.DECLARED_COUNT_MISMATCH, Severity.INFO,
            f"The README says {relative_path} has {declared.row_count} rows, but {rows} were read.",
            file=source_file,
        ))
    return issues


def _merge_declared(groups: list[VariableGroup]) -> DeclaredCounts:
    var = next((g.declared_variable_count for g in groups if g.declared_variable_count is not None), None)
    row = next((g.declared_row_count for g in groups if g.declared_row_count is not None), None)
    return DeclaredCounts(variable_count=var, row_count=row)


def _documented_as(groups: list[VariableGroup]) -> str | None:
    return next((g.file_hint for g in groups if g.file_hint), None)


def _dataset_title(groups: list[VariableGroup]) -> str | None:
    return next((g.section_title for g in groups if g.section_title), None)


def _source_file(groups: list[VariableGroup]) -> str | None:
    """The documentation file whose definitions describe this dataset (for provenance)."""
    return next((g.readme_file for g in groups if g.readme_file), None)


# --------------------------------------------------------------------------- relationships
def detect_relationships(datasets: list[Dataset]) -> list[Relationship]:
    """Find datasets that share column names (candidate join keys) (DESIGN.md section 15)."""
    relationships: list[Relationship] = []
    for i in range(len(datasets)):
        for j in range(i + 1, len(datasets)):
            a, b = datasets[i], datasets[j]
            a_norm = {normalize_name(h): h for h in a.headers}
            b_norm = {normalize_name(h): h for h in b.headers}
            shared = sorted(a_norm[k] for k in (set(a_norm) & set(b_norm)))
            if len(shared) >= 2:
                ratio = len(shared) / min(len(a.headers), len(b.headers))
                relationships.append(Relationship(
                    type="shared_variables",
                    datasets=[a.id, b.id],
                    variables=shared,
                    confidence=round(ratio, 3),
                ))
    return relationships


def build_project(parsed_readmes: list[ParsedReadme]) -> tuple[Project, list[Issue]]:
    """Merge project metadata from all READMEs; flag conflicting scalar values (DESIGN.md section 18)."""
    issues: list[Issue] = []
    project = Project()
    if not parsed_readmes:
        return project, issues

    # Scalar fields: take the first non-empty; warn on a genuine conflict.
    scalar_fields = ["title", "description", "identifier", "license", "citation", "geographic_location"]
    for field_name in scalar_fields:
        chosen: str | None = None
        for readme in parsed_readmes:
            value = readme.project.values.get(field_name)
            if not value:
                continue
            if chosen is None:
                chosen = value
                setattr(project, field_name, value)
                if field_name in readme.project.sources:
                    project.sources[field_name] = _stamp(readme.project.sources[field_name], readme.file_name)
            elif isinstance(value, str) and value.strip() != chosen.strip():
                issues.append(Issue.make(
                    IssueCode.CONFLICTING_METADATA, Severity.INFO,
                    f"READMEs disagree on {field_name!r}; kept the first value.",
                    technical_detail=f"{chosen!r} vs {value!r}",
                    file=readme.file_name,
                ))

    # collection_period (compound) from the first readme that has it.
    for readme in parsed_readmes:
        cp = readme.project.values.get("collection_period")
        if cp is not None:
            project.collection_period = cp
            break

    # List fields: concatenate, de-duplicated, preserving order.
    for readme in parsed_readmes:
        project.creators.extend(readme.project.creators)
        project.contacts.extend(readme.project.contacts)
        project.funding.extend(f for f in readme.project.funding if f not in project.funding)
        project.related_publications.extend(
            p for p in readme.project.related_publications if p not in project.related_publications)
        project.related_datasets.extend(
            d for d in readme.project.related_datasets if d not in project.related_datasets)
        project.methodology.extend(readme.project.methodology)
        project.additional_fields.extend(readme.project.additional)

    project.creators = _dedupe_people(project.creators)
    project.contacts = _dedupe_people(project.contacts)
    return project, issues


def _stamp(source: Source, file_name: str) -> Source:
    return source if source.file else Source(**{**source.model_dump(), "file": file_name})


def _dedupe_people(people):
    seen = set()
    out = []
    for person in people:
        key = (person.name.strip().lower(), (person.orcid or "").strip())
        if key[0] and key not in seen:
            seen.add(key)
            out.append(person)
    return out
