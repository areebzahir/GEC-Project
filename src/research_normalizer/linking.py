"""Link README variable groups to the actual data files they describe.

READMEs name the original upload (``survey.csv``) while the repository may hold a converted archival
copy (``survey.tab``) — Dataverse does exactly this (Ref [16]). We match a group's filename hint to a
data file by name *stem*: exact, then case-insensitive, then fuzzy >= 90 (DESIGN.md section 11). A
group with no filename hint, or no match, applies repository-wide.
"""

from __future__ import annotations

from pathlib import Path

from rapidfuzz import fuzz

from .readme.parser import ParsedReadme, VariableGroup

# Fuzzy stems only catch small renames (a typo, a "_v2"). Exact and case-insensitive stems are tried
# first, so similarly named files (survey_2018 / survey_2019, ratio ~91) can only be confused when the
# file the README names is missing altogether. Below the threshold a group falls back to
# repository-wide, which is the safer failure.
_STEM_FUZZY_THRESHOLD = 90.0


def link_groups_to_datasets(
    parsed_readmes: list[ParsedReadme],
    dataset_files: list[str],
) -> tuple[dict[str, list[VariableGroup]], list[VariableGroup]]:
    """Return (per-file groups keyed by dataset file path, repository-wide groups).

    ``dataset_files`` are the relative paths of the actual tabular files in the repository.
    """
    stems = {path: Path(path).stem for path in dataset_files}
    per_file: dict[str, list[VariableGroup]] = {path: [] for path in dataset_files}
    global_groups: list[VariableGroup] = []

    for readme in parsed_readmes:
        for group in readme.variable_groups:
            target = _resolve(group.file_hint, stems)
            if target is None:
                global_groups.append(group)
            else:
                per_file[target].append(group)
    return per_file, global_groups


def _resolve(file_hint: str | None, stems: dict[str, str]) -> str | None:
    """Find the dataset file path whose stem best matches the README's filename hint."""
    if not file_hint:
        return None
    hint_stem = Path(file_hint).stem

    # Exact stem.
    for path, stem in stems.items():
        if stem == hint_stem:
            return path
    # Case-insensitive stem.
    low = hint_stem.lower()
    for path, stem in stems.items():
        if stem.lower() == low:
            return path
    # Fuzzy stem.
    best_path, best_score = None, 0.0
    for path, stem in stems.items():
        score = fuzz.ratio(low, stem.lower())
        if score > best_score:
            best_path, best_score = path, score
    return best_path if best_score >= _STEM_FUZZY_THRESHOLD else None
