"""One-pass column statistics.

After a column's type is known, compute a small profile (count, missing, distinct, min/max/mean and
a few top values) with vectorized Polars expressions (DESIGN.md section 13). These feed the output
and also give the matcher instance-level evidence (e.g. observed distinct values vs documented
value labels).
"""

from __future__ import annotations

import polars as pl

from ..schema.document import ColumnStatistics, ColumnType


def profile_column(
    series: pl.Series,
    column_type: ColumnType,
    missing: set[str],
    top_n: int = 10,
) -> ColumnStatistics:
    """Compute statistics for one already-typed string column."""
    stripped = series.str.strip_chars()
    present = stripped.filter(
        ~stripped.is_in(list(missing)) & stripped.is_not_null() & (stripped != "")
    )
    total = series.len()
    count = present.len()
    missing_count = total - count
    distinct = present.n_unique()

    stats = ColumnStatistics(count=count, missing=missing_count, distinct=distinct)
    if count == 0:
        return stats

    if column_type in (ColumnType.INTEGER, ColumnType.NUMBER):
        numeric = present.cast(pl.Float64, strict=False).drop_nulls()
        if not numeric.is_empty():
            stats.min = float(numeric.min())
            stats.max = float(numeric.max())
            stats.mean = round(float(numeric.mean()), 6)
            if numeric.len() > 1:
                stats.std = round(float(numeric.std()), 6)
    elif column_type in (ColumnType.DATE, ColumnType.DATETIME):
        stats.min = str(present.min())
        stats.max = str(present.max())
    else:
        # Categorical/text: show the most common values as a quick preview.
        top = (
            present.value_counts(sort=True)
            .head(top_n)
            .get_column(present.name)
            .to_list()
        )
        stats.top_values = [str(v) for v in top]
    return stats
