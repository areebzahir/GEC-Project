"""The orchestrator: runs every stage in order and emits events.

This is the only module that knows the stage order (DESIGN.md section 8). Each stage is wrapped in a
boundary that converts an unexpected exception into an ``error`` issue so one bad file never aborts
the run (DESIGN.md section 18). Returns a validated :class:`RepositoryDocument`.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION, TOOL_VERSION
from .assemble import build_dataset, build_project, detect_relationships
from .config import PipelineConfig
from .discovery import DiscoveredFile, DiscoveryResult, FileRole, discover
from .events import EventEmitter, EventSink, ListSink, Stage
from .issues import Issue, IssueCode, IssueCollector, Severity
from .linking import link_groups_to_datasets
from .readme.parser import ParsedReadme, parse_readme
from .schema.document import (
    Dataset,
    DocumentInfo,
    DocumentSection,
    FileRecord,
    Processing,
    Project,
    RepositoryDocument,
    RepositoryInfo,
    Summary,
)
from .tabular.base import RawTable, read_table


def run_pipeline(
    input_path: str | Path,
    config: PipelineConfig | None = None,
    sink: EventSink | None = None,
) -> RepositoryDocument:
    """Process a repository into one standardized, validated document.

    ``sink`` receives progress events for a CLI or future GUI; if omitted, events are collected and
    discarded. The returned document is always schema-valid (it is a validated Pydantic model).
    """
    config = config or PipelineConfig()
    emitter = EventEmitter(sink or ListSink())
    issues = IssueCollector()
    stage_ms: dict[str, int] = {}
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()

    # --- discovery ---------------------------------------------------------
    emitter.emit(Stage.DISCOVERY, "started", "Scanning the repository for files")
    with _timed(stage_ms, Stage.DISCOVERY):
        result = discover(Path(input_path), config, issues)
    if result is None:
        return _fatal_document(input_path, issues, started, t0, stage_ms)

    with result:
        return _run_with_files(result, config, emitter, issues, started, t0, stage_ms)


def _run_with_files(
    result: DiscoveryResult,
    config: PipelineConfig,
    emitter: EventEmitter,
    issues: IssueCollector,
    started: datetime,
    t0: float,
    stage_ms: dict[str, int],
) -> RepositoryDocument:
    doc_files = [f for f in result.files if f.role is FileRole.DOCUMENTATION]
    data_files = [f for f in result.files if f.role is FileRole.TABULAR]
    other_files = [f for f in result.files if f.role is FileRole.OTHER]
    emitter.emit(
        Stage.CLASSIFICATION, "completed",
        f"Found {len(data_files)} data file(s) and {len(doc_files)} documentation file(s)",
        documentation=len(doc_files), tabular=len(data_files), other=len(other_files),
    )
    for f in other_files:
        issues.add(Issue.make(
            IssueCode.UNSUPPORTED_FILE, Severity.INFO,
            f"Skipped unsupported file: {f.relative_path}.", file=f.relative_path,
        ))
    if not doc_files:
        issues.add(Issue.make(
            IssueCode.README_NOT_FOUND, Severity.WARNING,
            "No README or documentation file was found; variables cannot be described.",
            suggestion="Add a README describing the datasets and their variables.",
        ))
    elif len(doc_files) > 1:
        issues.add(Issue.make(
            IssueCode.README_MULTIPLE, Severity.INFO,
            f"Found {len(doc_files)} documentation files; each is linked to the data it names.",
        ))

    # --- read data structures first (so READMEs can be matched against real columns) ------------
    emitter.emit(Stage.STRUCTURE_DETECTION, "started", "Reading data files and detecting their structure")
    tables: list[tuple[DiscoveredFile, RawTable]] = []
    with _timed(stage_ms, Stage.STRUCTURE_DETECTION):
        for index, file in enumerate(data_files, start=1):
            emitter.emit(
                Stage.STRUCTURE_DETECTION, "progress", f"Reading {file.relative_path}",
                within_stage=index / max(len(data_files), 1), file=file.relative_path,
            )
            for table in _safe(lambda f=file: read_table(f, config), file, issues, Stage.STRUCTURE_DETECTION):
                issues.extend(table.issues)
                tables.append((file, table))

    all_columns = frozenset(h for _, t in tables for h in t.headers)

    # --- parse READMEs -----------------------------------------------------
    emitter.emit(Stage.README_PARSING, "started", "Reading documentation")
    parsed_readmes: list[ParsedReadme] = []
    with _timed(stage_ms, Stage.README_PARSING):
        for file in doc_files:
            parsed = _first(_safe(
                lambda f=file: [parse_readme(f.path, f.extension, config, all_columns)],
                file, issues, Stage.README_PARSING,
            ))
            if parsed is not None:
                issues.extend(parsed.issues)
                parsed_readmes.append(parsed)
    emitter.emit(Stage.README_PARSING, "completed", f"Parsed {len(parsed_readmes)} documentation file(s)")

    # --- link README variable groups to the data files they describe -------
    data_rel_paths = [f.relative_path for f in data_files]
    per_file_groups, global_groups = link_groups_to_datasets(parsed_readmes, data_rel_paths)
    aliases = {k: v for r in parsed_readmes for k, v in r.abbreviations.items()}

    # --- build datasets (variable extraction + matching + normalization) ---
    emitter.emit(Stage.VARIABLE_MATCHING, "started", "Matching documented variables to data columns")
    datasets: list[Dataset] = []
    failed_files: set[str] = set()
    with _timed(stage_ms, Stage.VARIABLE_MATCHING):
        for file, table in tables:
            if table.frame.width == 0:
                # Sentinel table (empty/corrupt sheet): its issues were already collected.
                failed_files.add(file.relative_path)
                continue
            groups = list(per_file_groups.get(file.relative_path, [])) + list(global_groups)
            built = _first(_safe(
                lambda t=table, f=file, g=groups: [build_dataset(t, f.relative_path, g, aliases, config)],
                file, issues, Stage.VARIABLE_MATCHING,
            ))
            if built is not None:
                dataset, ds_issues = built
                issues.extend(ds_issues)
                datasets.append(dataset)
    datasets.sort(key=lambda d: d.file)

    # Stamp each dataset's last_updated from the generation date of a README that describes it.
    _apply_last_updated(datasets, parsed_readmes, per_file_groups)

    # --- relationships -----------------------------------------------------
    emitter.emit(Stage.NORMALIZATION, "started", "Looking for relationships between datasets")
    with _timed(stage_ms, Stage.NORMALIZATION):
        relationships = detect_relationships(datasets)
        relationships.extend(_documented_links(parsed_readmes))
        project, project_issues = build_project(parsed_readmes)
        issues.extend(project_issues)

    documents = _document_infos(parsed_readmes, doc_files, per_file_groups)

    # --- assemble + validate ----------------------------------------------
    emitter.emit(Stage.VALIDATION, "started", "Assembling and validating the result")
    files = _file_records(result.files, {d.file for d in datasets}, failed_files)
    summary = _summary(datasets, issues)
    duration_ms = int((time.perf_counter() - t0) * 1000)
    processing = _processing(config, started, duration_ms, stage_ms)

    document = RepositoryDocument(
        schema_version=SCHEMA_VERSION,
        repository=RepositoryInfo(name=result.repository_name, file_count=len(result.files)),
        project=project,
        documents=documents,
        datasets=datasets,
        relationships=relationships,
        files=files,
        issues=issues.issues,
        summary=summary,
        processing=processing,
    )
    emitter.emit(
        Stage.COMPLETE, "completed",
        f"Done: {summary.datasets} dataset(s), {summary.matched}/{summary.variables} variables matched, "
        f"{summary.warnings} warning(s)",
        datasets=summary.datasets, variables=summary.variables, matched=summary.matched,
        warnings=summary.warnings, errors=summary.errors,
    )
    return document


# --------------------------------------------------------------------------- helpers
class _timed:
    """Context manager that records a stage's wall-clock duration in milliseconds."""

    def __init__(self, store: dict[str, int], stage: Stage) -> None:
        self._store = store
        self._stage = stage

    def __enter__(self) -> "_timed":
        self._t = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self._store[self._stage.value] = self._store.get(self._stage.value, 0) + int(
            (time.perf_counter() - self._t) * 1000
        )


def _safe(func, file: DiscoveredFile, issues: IssueCollector, stage: Stage):
    """Run a stage function, converting any unexpected exception into an error issue.

    Returns the function's result (an iterable) or an empty list on failure, so the caller can keep
    going with the next file (DESIGN.md section 18).
    """
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - this is the safety boundary for third-party readers
        issues.add(Issue.make(
            IssueCode.INTERNAL_ERROR, Severity.ERROR,
            f"Could not process {file.relative_path} ({stage.value}).",
            technical_detail=f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}",
            file=file.relative_path,
        ))
        return []


def _first(items):
    items = list(items)
    return items[0] if items else None


def _documented_links(parsed_readmes: list[ParsedReadme]):
    from .schema.document import Relationship

    links = []
    seen: set[str] = set()
    for readme in parsed_readmes:
        for url in readme.project.related_datasets:
            if url not in seen:
                seen.add(url)
                links.append(Relationship(type="documented_link", target=url, **{"from": "project"}))
    return links


def _apply_last_updated(datasets, parsed_readmes, per_file_groups) -> None:
    """Set each dataset's last_updated to the generation date of a README that describes it."""
    gen_by_readme = {r.file_name: r.project.values.get("readme_generated_on") for r in parsed_readmes}
    for dataset in datasets:
        for group in per_file_groups.get(dataset.file, []):
            gen = gen_by_readme.get(group.readme_file)
            if gen:
                dataset.last_updated = gen
                break


def _document_infos(parsed_readmes, doc_files, per_file_groups) -> list[DocumentInfo]:
    by_name = {f.relative_path.split("/")[-1]: f for f in doc_files}
    # Which datasets each readme describes, from the file hints on its groups.
    describes_by_readme: dict[str, list[str]] = {}
    for path, groups in per_file_groups.items():
        for group in groups:
            if group.readme_file:
                describes_by_readme.setdefault(group.readme_file, [])
                if path not in describes_by_readme[group.readme_file]:
                    describes_by_readme[group.readme_file].append(path)

    infos: list[DocumentInfo] = []
    for readme in parsed_readmes:
        disc = by_name.get(readme.file_name)
        infos.append(DocumentInfo(
            file=disc.relative_path if disc else readme.file_name,
            format=readme.doc_format,
            encoding=readme.encoding,
            describes=describes_by_readme.get(readme.file_name, []),
            sections=[DocumentSection(title=s.title, lines=(s.line_start, s.line_end))
                      for s in readme.sections if s.title],
        ))
    return infos


def _file_records(files, processed_paths: set[str], failed: set[str]) -> list[FileRecord]:
    records = []
    for f in files:
        if f.relative_path in failed:
            status = "failed"
        elif f.role is FileRole.OTHER:
            status = "skipped"
        else:
            status = "processed"
        records.append(FileRecord(
            path=f.relative_path, role=f.role.value, format=f.extension.lstrip(".") or "unknown",
            size_bytes=f.size_bytes, sha256=f.sha256, status=status,
        ))
    return sorted(records, key=lambda r: r.path)


def _summary(datasets: list[Dataset], issues: IssueCollector) -> Summary:
    variables = sum(len(d.variables) for d in datasets)
    matched = sum(1 for d in datasets for v in d.variables if v.match.status.value == "matched")
    undocumented = sum(1 for d in datasets for v in d.variables if v.match.status.value == "unmatched")
    return Summary(
        datasets=len(datasets), variables=variables, matched=matched, undocumented=undocumented,
        warnings=issues.count(Severity.WARNING), errors=issues.count(Severity.ERROR) + issues.count(Severity.FATAL),
    )


def _processing(config: PipelineConfig, started: datetime, duration_ms: int, stage_ms: dict[str, int]) -> Processing:
    if config.deterministic:
        return Processing(tool_version=TOOL_VERSION, schema_version=SCHEMA_VERSION)
    return Processing(
        tool_version=TOOL_VERSION, schema_version=SCHEMA_VERSION,
        started_at=started.isoformat(), duration_ms=duration_ms, stage_durations_ms=stage_ms,
    )


def _fatal_document(input_path, issues, started, t0, stage_ms) -> RepositoryDocument:
    """Return a minimal valid document when discovery fails fatally (so callers always get JSON)."""
    duration_ms = int((time.perf_counter() - t0) * 1000)
    return RepositoryDocument(
        schema_version=SCHEMA_VERSION,
        repository=RepositoryInfo(name=Path(str(input_path)).name or "repository", file_count=0),
        project=Project(),
        processing=Processing(tool_version=TOOL_VERSION, schema_version=SCHEMA_VERSION),
        issues=issues.issues,
        summary=Summary(errors=issues.count(Severity.FATAL) + issues.count(Severity.ERROR)),
    )
