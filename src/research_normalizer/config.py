"""Central configuration for the pipeline.

Every tunable value lives here, each with a one-line rationale, so a reader never has to hunt for a
magic number in the middle of an algorithm (DESIGN.md section 0, "Readability"). Values may be
overridden from a TOML file (``--config``) or individual CLI flags; see ``cli.py``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

# Tokens that mean "missing" in essentially every research dataset, applied to the raw strings
# before any type casting (Frictionless Table Schema order, Ref [14]). README-declared codes are
# added to this set per file; we never guess numeric sentinels like -99 unless the README says so.
DEFAULT_MISSING_VALUES: tuple[str, ...] = ("", "NA", "N/A", "n/a", "NaN", "null", "NULL", "None", ".")

# Delimiters and quote characters the dialect sniffer will try (Ref [10][11][25]). Anything outside
# these must be supplied explicitly; that is a deliberate, documented limit, not a silent failure.
CANDIDATE_DELIMITERS: tuple[str, ...] = (",", "\t", ";", "|")
CANDIDATE_QUOTE_CHARS: tuple[str | None, ...] = ('"', "'", None)

# File extensions we treat as tabular data vs. documentation. Extension is only a hint; content
# sniffing confirms it (DESIGN.md section 10).
DELIMITED_EXTENSIONS: frozenset[str] = frozenset({".csv", ".tsv", ".tab", ".txt", ".dat"})
EXCEL_EXTENSIONS: frozenset[str] = frozenset({".xlsx", ".xlsm", ".xlsb", ".xls", ".ods"})
DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({".md", ".txt", ".pdf", ".docx", ".rst"})


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Immutable settings bundle threaded through every stage.

    Frozen so a stage can never mutate shared config; use :meth:`merged` to derive a variant.
    """

    # --- matching thresholds (DESIGN.md section 14) ---
    match_accept_threshold: float = 0.85
    """At or above this confidence a variable match is accepted without a warning."""
    match_review_threshold: float = 0.70
    """Between review and accept the match is kept but flagged MATCH_LOW_CONFIDENCE."""
    ambiguity_margin: float = 0.05
    """If the best and second-best candidates are this close, the match is flagged ambiguous."""
    fuzzy_score_cutoff: float = 70.0
    """RapidFuzz ignores pairs below this (0-100); below review_threshold anyway, saves work."""

    # --- header detection (DESIGN.md section 12) ---
    header_scan_rows: int = 30
    """How many leading non-empty rows to score when locating the header."""
    header_min_score: float = 0.35
    """Below this best score we give up and synthesise column_1..n."""

    # --- type inference / profiling (DESIGN.md section 13) ---
    categorical_max_distinct: int = 20
    """A column with at most this many distinct values is tagged 'categorical'."""
    type_sample_examples: int = 5
    """How many failing example values to attach when a type is rejected."""

    # --- encoding (DESIGN.md section 10) ---
    encoding_min_confidence: float = 0.80
    """chardet guesses below this confidence are rejected in favour of the cp1252/latin-1 rungs."""
    decode_sample_bytes: int = 1_048_576
    """Bytes fed to the encoding detector (1 MiB)."""

    # --- records / size limits (DESIGN.md sections 15, 23) ---
    max_records_per_dataset: int | None = 50_000
    """Rows emitted per dataset in the JSON; None means all (see --all-records)."""
    max_file_bytes: int = 1024 * 1024 * 1024
    """Files above this are skipped with FILE_TOO_LARGE (1 GiB)."""
    max_files: int = 10_000
    """Safety cap on how many files one run will process."""
    max_pdf_pages: int = 200
    """PDF READMEs longer than this are truncated with a warning."""
    sample_bytes: int = 65_536
    """Bytes of a delimited file used for dialect + header sniffing (64 KiB)."""

    # --- zip safety (DESIGN.md section 23; Ref [18][20]) ---
    max_zip_members: int = 10_000
    max_zip_total_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB uncompressed cap
    max_zip_ratio: float = 100.0  # per-member compression ratio ceiling (zip-bomb guard)

    # --- behaviour flags ---
    process_hidden_sheets: bool = True
    """Hidden/very-hidden Excel sheets are still processed, just flagged."""
    deterministic: bool = False
    """Drop timestamps/durations from output so golden tests compare byte-for-byte."""

    missing_value_tokens: tuple[str, ...] = DEFAULT_MISSING_VALUES

    def merged(self, **overrides: object) -> "PipelineConfig":
        """Return a copy with the given fields replaced (used by CLI flag handling)."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean) if clean else self


# Only these keys may appear in a user TOML file. An unknown key is a user error we report loudly
# rather than silently ignore (DESIGN.md "no silent guessing").
_ALLOWED_TOML_KEYS: frozenset[str] = frozenset(
    f.name for f in PipelineConfig.__dataclass_fields__.values() if not f.name.startswith("_")
)


def load_config(path: Path | None) -> PipelineConfig:
    """Build a config from defaults, optionally overlaid with a TOML file.

    Raises ``ValueError`` with a clear message on an unknown or wrong-typed key, so a mistake in the
    config surfaces immediately instead of being silently dropped.
    """
    if path is None:
        return PipelineConfig()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = set(raw) - _ALLOWED_TOML_KEYS
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_TOML_KEYS))
        raise ValueError(f"Unknown config key(s): {', '.join(sorted(unknown))}. Allowed: {allowed}")
    # tuples arrive from TOML as lists; coerce the sequence fields back to tuples.
    for key in ("missing_value_tokens",):
        if key in raw and isinstance(raw[key], list):
            raw[key] = tuple(raw[key])
    return PipelineConfig(**raw)
