"""Assemble stage results into the canonical document.

Given a parsed table and the documentation scoped to it (``linking.RepositoryScope``), build a
``Dataset`` (variables, typed records, statistics, provenance). Columns are matched against their
own documentation first; a column left over may still be documented by a *same-name* definition
from another file's or repository-wide documentation. Documented variables left over are sorted
into intentionally absent, present in another file (a cross-file reference), or genuinely missing
(a phantom). Also builds the ``Project`` and the cross-dataset ``relationships``
(DESIGN.md sections 14, 15). Pure functions only: inputs in, models out, issues returned alongside.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import polars as pl

from .config import PipelineConfig
from .issues import Issue, IssueCode, Severity
from .linking import DatasetKey, RepositoryScope, ScopedDefinition, resolve_file
from .matching.assign import ColumnContext, MatchOutcome, match_variables
from .matching.normalize import normalize_name
from .matching.scorers import SAME_NAME_METHODS, ScoreResult
from .readme.parser import ParsedReadme
from .readme.variable_layouts import VariableDefinition
from .schema.document import (
    ColumnType,
    DeclaredCounts,
    Dataset,
    MatchStatus,
    Project,
    Relationship,
    UnmatchedVariable,
    Variable,
    VariableMatch,
)
from .schema.provenance import Source
from .tabular.base import RawTable
from .tabular.profiling import profile_column
from .tabular.type_inference import ColumnTypeResult, infer_column_type

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Polars dtypes that serialize cleanly to JSON per inferred column type.
_CAST_TARGET = {
    ColumnType.INTEGER: pl.Int64,
    ColumnType.NUMBER: pl.Float64,
    ColumnType.BOOLEAN: pl.Boolean,
}
_NUMERIC = (ColumnType.INTEGER, ColumnType.NUMBER)

# A definition reused from another file's documentation: the same name up to letter case is as
# reliable as in-scope; a punctuation-only match ("site-id" vs "site_id") is worth a human look.
OUT_OF_SCOPE_NORMALIZED_PENALTY = 0.05
# Distinct raw values kept per column as matching evidence (value labels); bigger is not a code list.
MAX_CONTEXT_VALUES = 200
# Shared variables: at least this share of the smaller distinct-value set must appear in the other.
MIN_VALUE_OVERLAP = 0.2


def slugify(text: str) -> str:
    """A deterministic lowercase slug used for dataset ids."""
    return _SLUG_RE.sub("_", text.strip().lower()).strip("_") or "dataset"


@dataclass(slots=True)
class BuiltDataset:
    """One built dataset plus what it contributes to the repository level."""

    dataset: Dataset
    issues: list[Issue]
    relationships: list[Relationship] = field(default_factory=list)


@dataclass(slots=True)
class _Column:
    """A typed, profiled column and its matching context."""

    name: str
    position: int
    type_result: ColumnTypeResult
    stats: object
    missing: set[str]
    context: ColumnContext


def build_dataset(
    table: RawTable,
    relative_path: str,
    scope: RepositoryScope,
    aliases: dict[str, str],
    config: PipelineConfig,
) -> BuiltDataset:
    """Build one canonical ``Dataset`` from a parsed table and the documentation scoped to it."""
    key: DatasetKey = (relative_path, table.worksheet_name)
    dataset_id = slugify(table.source_name)
    frame = table.frame
    bound = scope.scoping.bound_to(key)
    scoped = scope.in_scope(key)
    dataset_missing = scope.missing_codes_for(key)

    columns = [_profile(table, name, pos, relative_path, dataset_missing, config)
               for pos, name in enumerate(frame.columns)]
    outcome = match_variables([c.context for c in columns], [sd.definition for sd in scoped], aliases, config)

    issues: list[Issue] = []
    variables: list[Variable] = []
    for col in columns:
        match, sd = _resolve_column(col.name, outcome, scoped, scope, key, config)
        if sd is not None:
            # Per-variable missing codes ("-99 = not measured") change the type: re-infer this column.
            col = _retype(table, col, sd.definition, relative_path, config)
        variables.append(_variable(table, col, match, sd, relative_path))
        issues.extend(col.type_result.issues)
        issues.extend(_variable_issues(col.name, match, sd, relative_path, config))

    phantoms, leftover_issues, relationships = _leftover_definitions(
        outcome, scoped, scope, key, dataset_id, relative_path)
    issues.extend(leftover_issues)

    declared = DeclaredCounts(
        variable_count=next((b.group.declared_variable_count for b in bound
                             if b.group.declared_variable_count is not None), None),
        row_count=next((b.group.declared_row_count for b in bound
                        if b.group.declared_row_count is not None), None),
    )
    source_file = bound[0].readme.file_name if bound else None
    issues.extend(_count_checks(declared, frame.width, frame.height, relative_path, source_file))

    records, included, truncated = _records(frame, {c.name: c.type_result.type for c in columns},
                                            {c.name: c.missing for c in columns}, config)
    dataset = Dataset(
        id=dataset_id,
        title=next((b.group.section_title for b in bound if b.group.section_title), None) or table.source_name,
        file=relative_path,
        documented_as=next((b.group.file_hint for b in bound if b.method == "file_hint" and b.group.file_hint), None),
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
        unmatched_documented_variables=phantoms,
        records=records,
        records_included=included,
        records_truncated=truncated,
    )
    return BuiltDataset(dataset, issues, relationships)


# --------------------------------------------------------------------------- columns
def _profile(table: RawTable, name: str, position: int, relative_path: str,
             missing_codes: set[str], config: PipelineConfig) -> _Column:
    """Infer a column's type and profile it, honouring the given README missing codes."""
    series = table.frame.get_column(name)
    type_result = infer_column_type(series, name, relative_path, missing_codes, config)
    all_missing = set(config.missing_value_tokens) | missing_codes
    stats = profile_column(series, type_result.type, all_missing)
    distinct = (frozenset(_distinct_values(series, all_missing))
                if stats.distinct <= MAX_CONTEXT_VALUES else frozenset())
    context = ColumnContext(
        name=name, position=position, column_type=type_result.type,
        distinct_values=distinct, distinct_count=stats.distinct,
        header_unit=table.header_units.get(name),
    )
    return _Column(name, position, type_result, stats, all_missing, context)


def _distinct_values(series: pl.Series, missing: set[str]) -> list[str]:
    stripped = series.str.strip_chars().drop_nulls()
    return [v for v in stripped.unique().to_list() if v and v not in missing]


def _retype(table: RawTable, col: _Column, defn: VariableDefinition, relative_path: str,
            config: PipelineConfig) -> _Column:
    extra = set(defn.missing_codes) - col.missing
    if not extra:
        return col
    codes = (col.missing - set(config.missing_value_tokens)) | extra
    return _profile(table, col.name, col.position, relative_path, codes, config)


def _resolve_column(name: str, outcome: MatchOutcome, scoped: list[ScopedDefinition],
                    scope: RepositoryScope, key: DatasetKey,
                    config: PipelineConfig) -> tuple[VariableMatch, ScopedDefinition | None]:
    """The column's match: in-scope first, else a same-name definition from elsewhere."""
    match = outcome.matches[name]
    di = outcome.assigned.get(name)
    if di is not None:
        match.evidence.insert(0, scoped[di].binding.scope_note())
        return match, scoped[di]
    found = scope.out_of_scope(name, key)
    if found is None:
        return match, None
    sd, link = found
    return _out_of_scope_match(sd, link, config), sd


def _out_of_scope_match(sd: ScopedDefinition, link: ScoreResult, config: PipelineConfig) -> VariableMatch:
    """Document a column from a definition written for another file (or the whole repository)."""
    d = sd.definition
    confidence = link.score if link.method in SAME_NAME_METHODS else link.score - OUT_OF_SCOPE_NORMALIZED_PENALTY
    where = "repository-wide documentation" if sd.binding.is_fallback else f"the documentation of {_target_label(sd.binding.target)}"
    return VariableMatch(
        status=MatchStatus.MATCHED,
        documented_name=d.name,
        confidence=round(confidence, 4),
        method="repository_definition" if sd.binding.is_fallback else "cross_file_definition",
        evidence=[
            sd.binding.scope_note(),
            f"not documented for this file; reused the definition of '{d.name}' from {where} "
            f"({sd.binding.readme.file_name}, lines {d.line_start}-{d.line_end})",
            *link.evidence,
        ],
        warning=None if confidence >= config.match_accept_threshold else "Low-confidence match; please verify.",
    )


def _variable(table: RawTable, col: _Column, match: VariableMatch, sd: ScopedDefinition | None,
              relative_path: str) -> Variable:
    defn = sd.definition if sd else None
    sources: dict[str, Source] = {}
    if sd is not None:
        sources["description"] = _definition_source(sd)
    unit = defn.unit if defn else None
    header_unit = table.header_units.get(col.name)
    if not unit and header_unit:
        # The data file itself states the unit (a units row under the header).
        unit = header_unit
        sources["unit"] = Source(file=relative_path, section=table.worksheet_name, method="header_units")
    position = col.position
    return Variable(
        name=col.name,
        original_name=table.original_headers[position] if position < len(table.original_headers) else col.name,
        position=position,
        label=defn.description if defn else None,
        description=defn.description if defn else None,
        unit=unit,
        type=col.type_result.type,
        format=col.type_result.format,
        role=col.type_result.role,
        missing_values=col.type_result.missing_values,
        value_labels=defn.value_labels if defn else [],
        notes=defn.notes if defn else None,
        statistics=col.stats,
        match=match,
        sources=sources,
    )


def _definition_source(sd: ScopedDefinition) -> Source:
    d = sd.definition
    return Source(file=sd.binding.readme.file_name, section=sd.binding.group.section_title,
                  lines=(d.line_start, d.line_end), method=d.method or None)


def _target_label(key: DatasetKey | None) -> str:
    if key is None:
        return "the repository"
    return f"{key[0]} [{key[1]}]" if key[1] else key[0]


# --------------------------------------------------------------------------- leftover definitions
def _leftover_definitions(outcome, scoped, scope, key, dataset_id, relative_path):
    """Sort documented-but-unmatched variables: intentionally absent, elsewhere, or a phantom."""
    phantoms: list[UnmatchedVariable] = []
    issues: list[Issue] = []
    relationships: list[Relationship] = []
    for di in outcome.unmatched_indices:
        sd = scoped[di]
        d = sd.definition
        readme_file = sd.binding.readme.file_name
        lines = (d.line_start, d.line_end)
        if d.absent:
            issues.append(Issue.make(
                IssueCode.DOCUMENTED_VARIABLE_INTENTIONALLY_ABSENT, Severity.INFO,
                f"The README documents {d.name!r} for {relative_path} but says it is not included.",
                technical_detail=d.absent_reason, file=readme_file, lines=lines,
            ))
            continue
        others = scope.other_datasets_with(d.name, key)
        if others:
            labels = ", ".join(_target_label(k) for k in others)
            issues.append(Issue.make(
                IssueCode.DOCUMENTED_VARIABLE_IN_OTHER_FILE, Severity.INFO,
                f"The README documents {d.name!r} next to {relative_path}, but it is a column of {labels}.",
                technical_detail=sd.binding.scope_note(), file=readme_file, lines=lines,
            ))
            relationships.append(Relationship(
                type="cross_file_reference",
                datasets=[dataset_id, *(scope.dataset_ids[k] for k in others if k in scope.dataset_ids)],
                variables=[d.name],
                source=_definition_source(sd),
                evidence=[sd.binding.scope_note(), f"'{d.name}' is a column of {labels}"],
            ))
            continue
        phantoms.append(UnmatchedVariable(name=d.name, description=d.description, lines=lines, file=readme_file))
        issues.append(Issue.make(
            IssueCode.DOCUMENTED_VARIABLE_NOT_IN_DATA, Severity.WARNING,
            f"The README describes a variable {d.name!r} that does not appear in {relative_path}.",
            technical_detail=f"No column scored at or above the review threshold; {sd.binding.scope_note()}.",
            file=readme_file, lines=lines,
            suggestion="Check whether the column was renamed or removed from the data file.",
        ))
    return phantoms, issues, relationships


def unassigned_documentation_issues(scope: RepositoryScope) -> list[Issue]:
    """One INFO per repository-wide documented variable that names no column in any file."""
    return [
        Issue.make(
            IssueCode.DOCUMENTATION_UNASSIGNED, Severity.INFO,
            f"The README documents {sd.definition.name!r}, but no data file has such a column and the "
            "section does not say which file it describes.",
            technical_detail=sd.binding.scope_note(),
            file=sd.binding.readme.file_name, lines=(sd.definition.line_start, sd.definition.line_end),
        )
        for sd in scope.unassigned_fallback()
    ]


# --------------------------------------------------------------------------- records + issues
def _records(
    frame: pl.DataFrame,
    typed: dict[str, ColumnType],
    missing: dict[str, set[str]],
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
        col = pl.when(col.is_in(list(missing[name])) | (col == "")).then(None).otherwise(col)
        target = _CAST_TARGET.get(typed[name])
        if target is not None:
            col = col.cast(target, strict=False)
        exprs.append(col.alias(name))
    cast_frame = view.select(exprs)
    return cast_frame.to_dicts(), view.height, truncated


def _variable_issues(name, match, sd, relative_path, config) -> list[Issue]:
    issues: list[Issue] = []
    if match.status is MatchStatus.UNMATCHED and sd is None:
        issues.append(Issue.make(
            IssueCode.VARIABLE_UNDOCUMENTED, Severity.WARNING,
            f"Column {name!r} in {relative_path} has no description in the README.",
            file=relative_path, column=name,
            suggestion="Add a definition for this variable to the README.",
        ))
    elif match.status is MatchStatus.AMBIGUOUS:
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


# --------------------------------------------------------------------------- relationships
def detect_relationships(datasets: list[Dataset]) -> list[Relationship]:
    """Datasets sharing variables: same normalized name, compatible types, overlapping values.

    A shared name alone is not enough (``pC1`` percentages vs ``PC1`` absorbances share a name, not
    a meaning), so the observed values must overlap too (DESIGN.md section 17).
    """
    values = {d.id: _value_sets(d) for d in datasets}
    relationships: list[Relationship] = []
    for i, a in enumerate(datasets):
        for b in datasets[i + 1:]:
            shared = _shared_variables(a, b, values[a.id], values[b.id])
            if shared:
                relationships.append(Relationship(
                    type="shared_variables",
                    datasets=[a.id, b.id],
                    variables=sorted(name for name, _, _ in shared),
                    confidence=round(sum(ov for _, ov, _ in shared) / len(shared), 3),
                    evidence=[note for _, _, note in shared],
                ))
    return relationships


def _shared_variables(a: Dataset, b: Dataset, a_values, b_values) -> list[tuple[str, float, str]]:
    b_by_norm = {normalize_name(v.name): v for v in b.variables}
    shared = []
    for va in a.variables:
        vb = b_by_norm.get(normalize_name(va.name))
        if vb is None or not _compatible_types(va.type, vb.type):
            continue
        sa, sb = a_values.get(va.name, set()), b_values.get(vb.name, set())
        smaller = min(len(sa), len(sb))
        common = len(sa & sb)
        if smaller and common / smaller >= MIN_VALUE_OVERLAP:
            note = f"{va.name} / {vb.name}: {common} of {smaller} distinct values in common"
            shared.append((va.name, common / smaller, note))
    return shared


def _compatible_types(a: ColumnType, b: ColumnType) -> bool:
    return a == b or (a in _NUMERIC and b in _NUMERIC)


def _value_sets(dataset: Dataset) -> dict[str, set[str]]:
    """Distinct non-null values per column from the typed records (numbers compared as floats)."""
    sets: dict[str, set[str]] = {h: set() for h in dataset.headers}
    for record in dataset.records:
        for name, value in record.items():
            if value is not None:
                sets[name].add(_value_key(value))
    return sets


def _value_key(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(float(value))
    return str(value).strip()


def documented_joins(parsed_readmes: list[ParsedReadme], datasets: list[Dataset]) -> list[Relationship]:
    """Relationships a README states explicitly, resolved from its filenames to dataset ids."""
    files = list(dict.fromkeys(d.file for d in datasets))
    out: list[Relationship] = []
    for readme in parsed_readmes:
        for rel in readme.relationships:
            ids: list[str] = []
            unresolved: list[str] = []
            for name in rel.files:
                path = resolve_file(name, files)
                if path is None:
                    unresolved.append(name)
                ids.extend(d.id for d in datasets if d.file == path and d.id not in ids)
            if not ids:
                continue
            evidence = [rel.text] + ([f"no data file found for: {', '.join(unresolved)}"] if unresolved else [])
            out.append(Relationship(
                type="documented_join", datasets=ids, variables=list(rel.variables),
                source=Source(file=readme.file_name, lines=(rel.line_start, rel.line_end) if rel.line_start else None,
                              method="documented_relationship"),
                evidence=evidence,
            ))
    return out


# --------------------------------------------------------------------------- project
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
