"""Scope README variable groups to the datasets they describe.

Every ``VariableGroup`` (a README section's variable list, or a data dictionary) is assigned to at
most one dataset target, keyed by ``(file relative path, worksheet name or None)``, with a recorded
reason. The rules, tried in order (DESIGN.md sections 11, 14):

1. **File hint.** The README names a file. Its *stem* is matched to a data file: exact, then
   case-insensitive, then fuzzy >= 90 (READMEs name the original upload ``survey.csv`` while
   Dataverse stores ``survey.tab``, Ref [16]). In a workbook the sheet hint picks the sheet, else
   the sheet whose columns best cover the group.
2. **Documentation name.** ``survey_README.txt`` documents ``survey.csv``: the doc stem minus generic
   words (readme, codebook, ...) equals, is contained in, or closely matches one data stem.
3. **Content coverage.** The share of the group's variable names that link to a column of a
   dataset by name (exact / case / normalized / abbreviation, never weak fuzzy). The best dataset
   wins if it covers at least half the group and clearly beats the runner-up. A repository with a
   single dataset binds everything to it.
4. Anything else is **repository-wide fallback** documentation. It may only document columns
   through same-name matches and never produces "documented but absent" warnings per dataset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from .matching.normalize import normalize_name
from .matching.scorers import ALIAS_METHODS, STRONG_METHODS, ScoreResult, name_link, strong_link
from .readme.parser import ParsedReadme, VariableGroup
from .readme.variable_layouts import VariableDefinition

DatasetKey = tuple[str, str | None]

# Fuzzy stems only catch small renames (a typo, a "_v2"). Exact and case-insensitive stems are tried
# first, so similarly named files (survey_2018 / survey_2019, ratio ~91) can only be confused when the
# file the README names is missing altogether. Below the threshold the next rule is tried.
STEM_FUZZY_THRESHOLD = 90.0
# Content coverage: the group must link at least half its names to the dataset's columns, and beat
# the runner-up by this margin, so shared key columns alone never decide the binding.
COVERAGE_MIN = 0.5
COVERAGE_MARGIN = 0.2
# A documentation stem shorter than this after removing generic words ("data", "v2") is too vague
# to name a data file by containment.
MIN_DOC_CORE_LEN = 3
# Containment must cover at least this share of the longer name, so a generic core like "data" is
# not read as naming "cow_energy_balance_data".
MIN_CONTAINMENT_RATIO = 0.5
# Words that make a file name "documentation" rather than naming its subject.
_DOC_WORDS = ("read_me", "read-me", "readme", "codebook", "dictionary", "metadata",
              "documentation", "notes", "info")
_SEPARATORS = re.compile(r"[\s_\-.]+")
_LINKING_METHODS = STRONG_METHODS | ALIAS_METHODS


@dataclass(frozen=True, slots=True)
class DatasetTarget:
    """A table that documentation can describe: one delimited file or one worksheet."""

    file: str
    sheet: str | None
    columns: tuple[str, ...]

    @property
    def key(self) -> DatasetKey:
        return (self.file, self.sheet)


@dataclass(slots=True)
class GroupBinding:
    """Where a group of documented variables applies, and why (kept for provenance)."""

    group: VariableGroup
    readme: ParsedReadme
    target: DatasetKey | None   # None = repository-wide fallback
    method: str                 # file_hint | documentation_name | coverage | single_dataset | fallback
    reason: str

    @property
    def is_fallback(self) -> bool:
        return self.target is None

    def scope_note(self) -> str:
        """Plain-language provenance: which README section, and why it applies here."""
        section = f" section '{self.group.section_title}'" if self.group.section_title else ""
        return f"documented in {self.readme.file_name}{section}; {self.reason}"


@dataclass(slots=True)
class Scoping:
    """The final assignment of every variable group."""

    bindings: list[GroupBinding]

    def bound_to(self, key: DatasetKey) -> list[GroupBinding]:
        return [b for b in self.bindings if b.target == key]

    @property
    def fallback(self) -> list[GroupBinding]:
        return [b for b in self.bindings if b.is_fallback]

    def describes(self, readme: ParsedReadme) -> list[DatasetKey]:
        """Dataset targets a parsed document was bound to, in first-seen order."""
        keys: list[DatasetKey] = []
        for b in self.bindings:
            if b.readme is readme and b.target and b.target not in keys:
                keys.append(b.target)
        return keys


def scope_groups(parsed_readmes: list[ParsedReadme], targets: list[DatasetTarget]) -> Scoping:
    """Assign every variable group of every README/dictionary to a dataset target (or fallback)."""
    bindings = [
        _bind(group, readme, targets)
        for readme in parsed_readmes
        for group in readme.variable_groups
    ]
    return Scoping(bindings)


def resolve_file(file_hint: str | None, files: list[str]) -> str | None:
    """The data file path whose stem best matches a README filename (exact, case, fuzzy)."""
    if not file_hint:
        return None
    hint = Path(file_hint).stem
    stems = {path: Path(path).stem for path in files}
    for same in (lambda s: s == hint, lambda s: s.lower() == hint.lower()):
        hits = [p for p, s in stems.items() if same(s)]
        if hits:
            return hits[0]
    scored = [(fuzz.ratio(hint.lower(), s.lower()), p) for p, s in stems.items()]
    best = max(scored, default=(0.0, None))
    return best[1] if best[0] >= STEM_FUZZY_THRESHOLD else None


# --------------------------------------------------------------------------- binding rules
def _bind(group: VariableGroup, readme: ParsedReadme, targets: list[DatasetTarget]) -> GroupBinding:
    files = _unique([t.file for t in targets])

    # 1. The README names the file (and maybe the sheet).
    hinted = resolve_file(group.file_hint, files)
    if hinted:
        target, why = _pick_within_file(group, readme, hinted, targets)
        if target:
            return GroupBinding(group, readme, target.key, "file_hint",
                                f"the README names '{group.file_hint}' ({why})")
    if group.sheet_hint and not group.file_hint:
        sheet = _resolve_sheet(group.sheet_hint, [t for t in targets if t.sheet])
        if sheet:
            return GroupBinding(group, readme, sheet.key, "file_hint",
                                f"the README names worksheet '{group.sheet_hint}'")

    # 2. The documentation file is named after a data file.
    if not group.file_hint:
        named = _file_named_by_doc(readme.file_name, files)
        if named:
            target, why = _pick_within_file(group, readme, named, targets)
            if target:
                return GroupBinding(group, readme, target.key, "documentation_name",
                                    f"documentation file {readme.file_name} is named after {named} ({why})")

    # 3. Content coverage, or the only dataset in the repository.
    best = _best_by_coverage(group, readme, targets)
    if best:
        target, cov = best
        return GroupBinding(group, readme, target.key, "coverage",
                            f"{cov:.0%} of its variable names match columns of {_label(target)}")
    if len(targets) == 1:
        return GroupBinding(group, readme, targets[0].key, "single_dataset",
                            "it is the only dataset in the repository")
    return GroupBinding(group, readme, None, "fallback",
                        "no data file could be identified for it, so it applies repository-wide")


def _pick_within_file(group, readme, file, targets) -> tuple[DatasetTarget | None, str]:
    """Choose the target inside one file: the only table, the hinted sheet, or best coverage."""
    tables = [t for t in targets if t.file == file]
    if len(tables) == 1:
        return tables[0], "file name match"
    if group.sheet_hint:
        sheet = _resolve_sheet(group.sheet_hint, tables)
        if sheet:
            return sheet, f"worksheet '{group.sheet_hint}'"
    scored = sorted(((coverage(group, t, readme.abbreviations), i) for i, t in enumerate(tables)),
                    key=lambda x: (-x[0], x[1]))
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        cov, i = scored[0]
        return tables[i], f"worksheet whose columns cover {cov:.0%} of the variables"
    return None, ""


def _resolve_sheet(hint: str, tables: list[DatasetTarget]) -> DatasetTarget | None:
    for same in (lambda s: s == hint, lambda s: s.lower() == hint.lower().strip()):
        for t in tables:
            if t.sheet and same(t.sheet):
                return t
    scored = [(fuzz.ratio(hint.lower(), t.sheet.lower()), i) for i, t in enumerate(tables) if t.sheet]
    best = max(scored, default=(0.0, -1))
    return tables[best[1]] if best[0] >= STEM_FUZZY_THRESHOLD else None


def _file_named_by_doc(doc_file: str, files: list[str]) -> str | None:
    """The data file a documentation file is named after ("survey_codebook.txt" -> "survey.csv")."""
    core = _doc_core(doc_file)
    if len(core) < MIN_DOC_CORE_LEN:
        return None
    cores = {path: _SEPARATORS.sub("", Path(path).stem.lower()) for path in files}
    # Strongest relation first; a level only counts if exactly one file satisfies it.
    for related in (
        lambda c: c == core,
        lambda c: _contains(core, c),
        lambda c: fuzz.ratio(core, c) >= STEM_FUZZY_THRESHOLD,
    ):
        hits = [p for p, c in cores.items() if related(c)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None  # several data files fit equally: do not guess
    return None


def _contains(a: str, b: str) -> bool:
    """One name contains the other, and is a substantial part of it ("survey" in "survey2019")."""
    short, long = sorted((a, b), key=len)
    return (len(short) >= MIN_DOC_CORE_LEN and short in long
            and len(short) / len(long) >= MIN_CONTAINMENT_RATIO)


def _doc_core(doc_file: str) -> str:
    stem = Path(doc_file).stem.lower()
    for word in _DOC_WORDS:
        stem = stem.replace(word, " ")
    return _SEPARATORS.sub("", stem)


def _best_by_coverage(group, readme, targets) -> tuple[DatasetTarget, float] | None:
    scored = sorted(((coverage(group, t, readme.abbreviations), i) for i, t in enumerate(targets)),
                    key=lambda x: (-x[0], x[1]))
    if not scored or scored[0][0] < COVERAGE_MIN:
        return None
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if scored[0][0] - runner_up < COVERAGE_MARGIN:
        return None
    return targets[scored[0][1]], scored[0][0]


def coverage(group: VariableGroup, target: DatasetTarget, aliases: dict[str, str] | None = None) -> float:
    """Fraction of the group's (present) variable names that link to a column by name."""
    names = [v.name for v in group.variables if not v.absent]
    if not names:
        return 0.0
    hits = sum(1 for name in names if _links_any(name, target.columns, aliases))
    return hits / len(names)


def _links_any(name: str, columns: tuple[str, ...], aliases) -> bool:
    for col in columns:
        result = name_link(col, name, aliases)
        if result is not None and result.method in _LINKING_METHODS:
            return True
    return False


def _label(target: DatasetTarget) -> str:
    return f"{target.file} [{target.sheet}]" if target.sheet else target.file


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


# --------------------------------------------------------------------------- repository-wide view
@dataclass(slots=True)
class ScopedDefinition:
    """A documented variable together with the binding that says where it applies."""

    binding: GroupBinding
    definition: VariableDefinition


class RepositoryScope:
    """Everything a dataset build needs to know about documentation *outside* its own scope.

    Lookups go through normalized names, because only same-name links (exact / case / punctuation)
    are trusted across files: a site id documented once for one file is the same site id in the
    next file, but an abbreviation guess across files is not.
    """

    def __init__(self, scoping: Scoping, targets: list[DatasetTarget],
                 dataset_ids: dict[DatasetKey, str], parsed_readmes: list[ParsedReadme]) -> None:
        self.scoping = scoping
        self.targets = targets
        self.dataset_ids = dataset_ids
        self._readmes = parsed_readmes
        self._defs_by_norm: dict[str, list[ScopedDefinition]] = {}
        for b in scoping.bindings:
            for d in b.group.variables:
                if not d.absent:
                    self._defs_by_norm.setdefault(normalize_name(d.name), []).append(ScopedDefinition(b, d))
        self._targets_by_norm: dict[str, list[DatasetKey]] = {}
        for t in targets:
            for col in t.columns:
                self._targets_by_norm.setdefault(normalize_name(col), []).append(t.key)

    def in_scope(self, key: DatasetKey) -> list[ScopedDefinition]:
        """Definitions bound to this dataset, de-duplicated by name (first wins, gaps filled)."""
        by_name: dict[str, ScopedDefinition] = {}
        for b in self.scoping.bound_to(key):
            for d in b.group.variables:
                if d.name in by_name:
                    _fill_gaps(by_name[d.name].definition, d)
                else:
                    by_name[d.name] = ScopedDefinition(b, d)
        return list(by_name.values())

    def out_of_scope(self, column: str, key: DatasetKey) -> tuple[ScopedDefinition, ScoreResult] | None:
        """The best same-name definition bound to another dataset or repository-wide."""
        best: tuple[ScopedDefinition, ScoreResult] | None = None
        for sd in self._defs_by_norm.get(normalize_name(column), []):
            if sd.binding.target == key:
                continue
            link = strong_link(column, sd.definition.name)
            if link is not None and (best is None or link.score > best[1].score):
                best = (sd, link)
        return best

    def other_datasets_with(self, name: str, key: DatasetKey) -> list[DatasetKey]:
        """Other datasets that have a column with the same name (up to case/punctuation)."""
        keys = self._targets_by_norm.get(normalize_name(name), [])
        return [k for k in dict.fromkeys(keys) if k != key]

    def missing_codes_for(self, key: DatasetKey) -> set[str]:
        """Missing-value codes declared for this dataset (its groups + their READMEs' global codes).

        A README that documents no particular dataset is repository-wide, so its global codes apply
        to every dataset.
        """
        bound = self.scoping.bound_to(key)
        codes = {c for b in bound for c in b.group.missing_codes}
        bound_readmes = {id(b.readme) for b in bound}
        for readme in self._readmes:
            describes_any = any(b.readme is readme and not b.is_fallback for b in self.scoping.bindings)
            if id(readme) in bound_readmes or not describes_any:
                codes.update(readme.global_missing_codes)
        return {c for c in codes if c is not None}

    def unassigned_fallback(self) -> list[ScopedDefinition]:
        """Repository-wide definitions whose name matches no column anywhere (reported once)."""
        out: list[ScopedDefinition] = []
        for b in self.scoping.fallback:
            for d in b.group.variables:
                if not self._targets_by_norm.get(normalize_name(d.name)):
                    out.append(ScopedDefinition(b, d))
        return out


def _fill_gaps(kept: VariableDefinition, extra: VariableDefinition) -> None:
    """A variable documented twice for the same dataset: keep the first, fill its empty fields."""
    for attr in ("description", "unit", "notes"):
        if getattr(kept, attr) is None and getattr(extra, attr):
            setattr(kept, attr, getattr(extra, attr))
    if not kept.value_labels and extra.value_labels:
        kept.value_labels = list(extra.value_labels)
    kept.missing_codes = list(dict.fromkeys([*kept.missing_codes, *extra.missing_codes]))
