"""Column type inference and missing-value handling (DESIGN.md section 13)."""

from __future__ import annotations

import polars as pl

from research_normalizer.config import PipelineConfig
from research_normalizer.schema.document import ColumnType
from research_normalizer.tabular.type_inference import infer_column_type

CFG = PipelineConfig()


def _infer(values, extra=None):
    return infer_column_type(pl.Series("c", values), "c", "f", extra or set(), CFG)


def test_integer():
    assert _infer(["1", "2", "300"]).type is ColumnType.INTEGER


def test_leading_zero_stays_string():
    assert _infer(["007", "012", "100"]).type is ColumnType.STRING


def test_zero_one_is_integer_not_boolean():
    assert _infer(["0", "1", "1", "0"]).type is ColumnType.INTEGER


def test_boolean_words():
    assert _infer(["yes", "no", "yes"]).type is ColumnType.BOOLEAN


def test_number_scientific():
    assert _infer(["9.16E+06", "1.2E7", "3E5"]).type is ColumnType.NUMBER


def test_iso_date():
    r = _infer(["2021-01-05", "2021-11-22", "2018-10-26"])
    assert r.type is ColumnType.DATE and r.format == "%Y-%m-%d"


def test_ambiguous_date_stays_string_with_warning():
    r = _infer(["01/02/2021", "03/04/2021", "05/06/2021"])
    assert r.type is ColumnType.STRING
    assert any(i.code.value == "AMBIGUOUS_DATE_FORMAT" for i in r.issues)


def test_declared_missing_code_applied():
    # -99 is numeric but declared missing -> column of real numbers stays numeric, -99 nulled.
    r = _infer(["1.5", "-99", "2.5", "-99", "3.5"], extra={"-99"})
    assert r.type is ColumnType.NUMBER
    assert "-99" in r.missing_values


def test_na_token_default_missing():
    r = _infer(["1.5", "NA", "2.5", "3.5"])
    assert r.type is ColumnType.NUMBER
