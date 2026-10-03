"""Infer a logical type per column, after applying missing-value codes.

Order follows Frictionless Table Schema (Ref [14]): missing values are turned into nulls on the raw
strings *before* any type casting. A type is accepted only if every non-missing value casts cleanly
(DESIGN.md section 13); otherwise the column stays ``string`` and we keep example failures. Casting
is done with vectorized Polars expressions, so this is fast even on millions of rows.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from ..config import POSSIBLE_MISSING_SENTINELS, PipelineConfig
from ..issues import Issue, IssueCode, Severity
from ..schema.document import ColumnRole, ColumnType

# Boolean words we accept. Plain 0/1 is intentionally NOT boolean (it is usually a coded integer).
_TRUE = frozenset({"true", "t", "yes", "y"})
_FALSE = frozenset({"false", "f", "no", "n"})
# Date formats tried, each unambiguous on its own. Ambiguous dd/mm vs mm/dd is detected separately.
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%Y", "%d/%m/%Y", "%m/%d/%Y")
_DATETIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%.f")
_LEADING_ZERO_RE = r"^0\d+$"  # 007 stays a string (identifier rule); Polars wants a pattern string


@dataclass(frozen=True, slots=True)
class ColumnTypeResult:
    """The inferred type for one column, plus provenance for any warning."""

    type: ColumnType
    format: str
    role: ColumnRole
    missing_values: list[str]
    failures: list[str]       # example values that blocked a more specific type
    issues: list[Issue]


def _non_missing(series: pl.Series, missing: set[str]) -> pl.Series:
    """Return the series with missing tokens removed (not nulled), as non-empty strings."""
    stripped = series.str.strip_chars()
    return stripped.filter(~stripped.is_in(list(missing)) & stripped.is_not_null() & (stripped != ""))


def _all_cast(values: pl.Series, dtype: pl.DataType) -> bool:
    if values.is_empty():
        return False
    casted = values.cast(dtype, strict=False)
    return casted.null_count() == 0


def _all_match_bool(values: pl.Series) -> bool:
    low = values.str.to_lowercase()
    return bool(low.is_in(list(_TRUE | _FALSE)).all())


def _parse_dates(values: pl.Series, formats: tuple[str, ...], is_datetime: bool) -> str | None:
    """Return the first format that parses every value, else None."""
    for fmt in formats:
        parsed = (
            values.str.to_datetime(fmt, strict=False)
            if is_datetime
            else values.str.to_date(fmt, strict=False)
        )
        if parsed.null_count() == 0:
            return fmt
    return None


def infer_column_type(
    series: pl.Series,
    column: str,
    file: str,
    extra_missing: set[str],
    config: PipelineConfig,
) -> ColumnTypeResult:
    """Infer the logical type of one string column (DESIGN.md section 13)."""
    missing = set(config.missing_value_tokens) | extra_missing
    values = _non_missing(series, missing)
    issues: list[Issue] = []
    # Report only the codes that actually occur in this column, so the output is honest about the
    # data rather than echoing the whole configured list. Blank cells are always missing and are
    # not listed as a "code".
    present_missing = _observed_missing_tokens(series, missing)

    if values.is_empty():
        return ColumnTypeResult(ColumnType.STRING, "default", ColumnRole.TEXT, present_missing, [], issues)

    distinct = values.n_unique()

    # Boolean (words only).
    if _all_match_bool(values):
        role = ColumnRole.CATEGORICAL
        return ColumnTypeResult(ColumnType.BOOLEAN, "default", role, present_missing, [], issues)

    # Leading-zero codes like "007" are identifiers, never numbers: a numeric cast would drop the
    # zeros and corrupt the value. Detecting this up front keeps them as strings (identifier rule).
    has_leading_zero = bool(values.filter(values.str.contains(_LEADING_ZERO_RE)).len())
    if not has_leading_zero:
        # Integer first (more specific than number).
        if _all_cast(values, pl.Int64):
            role = ColumnRole.IDENTIFIER if distinct == values.len() else ColumnRole.MEASURE
            issues.extend(_sentinel_warnings(values, column, file, missing, config))
            return ColumnTypeResult(ColumnType.INTEGER, "default", role, present_missing, [], issues)
        # Number (covers scientific notation like 9.16E+06).
        if _all_cast(values, pl.Float64):
            issues.extend(_sentinel_warnings(values, column, file, missing, config))
            return ColumnTypeResult(ColumnType.NUMBER, "default", ColumnRole.MEASURE, present_missing, [], issues)

    # Date / datetime, only for an unambiguous format.
    dt_fmt = _parse_dates(values, _DATETIME_FORMATS, is_datetime=True)
    if dt_fmt:
        return ColumnTypeResult(ColumnType.DATETIME, dt_fmt, ColumnRole.TEMPORAL, present_missing, [], issues)

    date_formats_that_fit = [f for f in _DATE_FORMATS if _date_fits(values, f)]
    if len(date_formats_that_fit) == 1:
        return ColumnTypeResult(ColumnType.DATE, date_formats_that_fit[0], ColumnRole.TEMPORAL, present_missing, [], issues)
    if len(date_formats_that_fit) > 1 and _ambiguous_day_month(date_formats_that_fit):
        issues.append(Issue.make(
            IssueCode.AMBIGUOUS_DATE_FORMAT, Severity.WARNING,
            f"Dates in {column!r} fit more than one day/month order; kept as text to avoid guessing.",
            technical_detail=f"formats that fit: {date_formats_that_fit}",
            file=file, column=column,
        ))

    # Fall back to string. Record a few example values that blocked numeric typing, which helps a
    # user see *why* (e.g. a stray 'n/a' not in the declared missing codes).
    failures = _blocking_examples(values, config.type_sample_examples)
    role = _text_role(distinct, values.len(), config)
    return ColumnTypeResult(ColumnType.STRING, "default", role, present_missing, failures, issues)


def _observed_missing_tokens(series: pl.Series, missing: set[str]) -> list[str]:
    """The non-blank missing tokens that occur in ``series``, sorted for deterministic output."""
    tokens = [t for t in missing if t]
    if not tokens:
        return []
    stripped = series.str.strip_chars()
    found = stripped.filter(stripped.is_in(tokens)).unique().to_list()
    return sorted(str(t) for t in found)


def _sentinel_warnings(
    values: pl.Series,
    column: str,
    file: str,
    missing: set[str],
    config: PipelineConfig,
) -> list[Issue]:
    """Warn when a typical missing-data code (-999, 9999, ...) sits far outside a numeric column.

    Such a value is very likely an undeclared missing code, but it could also be real, so we only
    warn (POSSIBLE_UNDECLARED_MISSING_CODE) and never null it. "Far" means more than
    ``sentinel_outlier_iqr_factor`` interquartile ranges from the median of the other values.
    """
    numbers = values.cast(pl.Float64, strict=False).drop_nulls()
    declared = {_as_float(t) for t in missing} - {None}
    issues: list[Issue] = []
    for sentinel in POSSIBLE_MISSING_SENTINELS:
        if sentinel in declared:
            continue  # the README already says this code means missing; it was nulled
        count = int((numbers == sentinel).sum())
        if count == 0:
            continue
        others = numbers.filter(~numbers.is_in(list(POSSIBLE_MISSING_SENTINELS)))
        if others.len() < config.sentinel_min_other_values:
            continue  # too few real values to tell an outlier from the normal range
        median = float(others.median())
        iqr = float(others.quantile(0.75, "linear") - others.quantile(0.25, "linear"))
        # A constant column has IQR 0; use a tiny floor so any far-off sentinel still stands out.
        spread = max(iqr, 1e-9)
        if abs(sentinel - median) <= config.sentinel_outlier_iqr_factor * spread:
            continue
        code = _format_sentinel(sentinel)
        issues.append(Issue.make(
            IssueCode.POSSIBLE_UNDECLARED_MISSING_CODE, Severity.WARNING,
            f"Column {column!r} contains {code} ({count} time(s)), far outside its other values; "
            "it may be an undeclared missing-data code.",
            technical_detail=(
                f"median={median:g}, IQR={iqr:g}, |{code} - median| > "
                f"{config.sentinel_outlier_iqr_factor:g} x IQR; values kept as-is"
            ),
            file=file, column=column,
            suggestion=f"If {code} means 'missing', declare it in the README's missing-data codes.",
        ))
    return issues


def _as_float(token: str) -> float | None:
    try:
        return float(token)
    except ValueError:
        return None


def _format_sentinel(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _date_fits(values: pl.Series, fmt: str) -> bool:
    return values.str.to_date(fmt, strict=False).null_count() == 0


def _ambiguous_day_month(formats: list[str]) -> bool:
    """True if both a day-first and a month-first format fit (genuinely ambiguous)."""
    day_first = any(f in ("%d-%m-%Y", "%d/%m/%Y") for f in formats)
    month_first = any(f in ("%m-%d-%Y", "%m/%d/%Y") for f in formats)
    return day_first and month_first


def _blocking_examples(values: pl.Series, n: int) -> list[str]:
    """A few values that fail to cast to float, as evidence for staying string."""
    casted = values.cast(pl.Float64, strict=False)
    mask = casted.is_null()
    bad = values.filter(mask)
    return bad.unique().head(n).to_list()


def _text_role(distinct: int, total: int, config: PipelineConfig) -> ColumnRole:
    if distinct <= config.categorical_max_distinct and distinct < total:
        return ColumnRole.CATEGORICAL
    if distinct == total:
        return ColumnRole.IDENTIFIER
    return ColumnRole.TEXT
