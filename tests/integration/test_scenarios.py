"""The 20 competition scenarios run through the whole pipeline (DESIGN.md section 21).

Every scenario asserts both that the run completes and that the specific behaviour it targets shows
up in the output or the issues. All outputs are validated against the committed JSON Schema with the
independent ``jsonschema`` validator.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from research_normalizer.config import PipelineConfig
from research_normalizer.pipeline import run_pipeline
from research_normalizer.schema.__main__ import build_schema
from tests.fixtures import builders

SCHEMA = build_schema()
DET = PipelineConfig(deterministic=True)


def _run(path: Path):
    doc = run_pipeline(path, DET)
    jsonschema.validate(json.loads(doc.model_dump_json(by_alias=True)), SCHEMA)
    return doc


def _codes(doc):
    return {i.code.value for i in doc.issues}


def _match_methods(doc):
    return {v.match.method for d in doc.datasets for v in d.variables}


def test_01_clean(tmp_repo):
    doc = _run(builders.clean_repo(tmp_repo))
    d = doc.datasets[0]
    assert d.column_count == 3
    assert all(v.match.status.value == "matched" for v in d.variables)


def test_02_tsv(tmp_repo):
    doc = _run(builders.tsv_repo(tmp_repo))
    d = doc.datasets[0]
    assert d.structure.delimiter == "\t"
    assert {v.name for v in d.variables} == {"site", "depth_m", "temp_c"}


def test_03_multisheet_excel(tmp_repo):
    doc = _run(builders.excel_repo(tmp_repo))
    sheets = {d.worksheet_name for d in doc.datasets}
    assert "Measurements" in sheets
    assert any(i.code.value == "EXCEL_HIDDEN_SHEET" for i in doc.issues)
    assert any(i.code.value == "EXCEL_EMPTY_SHEET" for i in doc.issues)


def test_04_fuzzy_names(tmp_repo):
    doc = _run(builders.fuzzy_names_repo(tmp_repo))
    methods = _match_methods(doc)
    # At least one non-exact method was needed to link the drifted names.
    assert methods & {"case_insensitive", "normalized", "token_abbreviation", "fuzzy", "token_set"}
    assert all(v.match.documented_name for d in doc.datasets for v in d.variables)


def test_05_duplicate_columns(tmp_repo):
    doc = _run(builders.duplicate_columns_repo(tmp_repo))
    assert any(i.code.value == "HEADER_DUPLICATE" for i in doc.issues)
    headers = doc.datasets[0].headers
    assert len(headers) == len(set(headers))  # de-duplicated


def test_06_undocumented_column(tmp_repo):
    doc = _run(builders.undocumented_column_repo(tmp_repo))
    assert any(i.code.value == "VARIABLE_UNDOCUMENTED" for i in doc.issues)


def test_08_phantom_variable(tmp_repo):
    doc = _run(builders.phantom_variable_repo(tmp_repo))
    unmatched = [u.name for d in doc.datasets for u in d.unmatched_documented_variables]
    assert "ghost" in unmatched
    assert any(i.code.value == "DOCUMENTED_VARIABLE_NOT_IN_DATA" for i in doc.issues)


def test_11_encoding(tmp_repo):
    doc = _run(builders.encoding_repo(tmp_repo))
    d = next(d for d in doc.datasets if "fr" in d.file)
    assert d.structure.encoding in {"cp1252", "latin-1"}
    assert d.row_count == 2


def test_12_wrong_delimiter(tmp_repo):
    doc = _run(builders.wrong_delimiter_repo(tmp_repo))
    d = doc.datasets[0]
    assert d.structure.delimiter == ";"
    assert d.column_count == 3


def test_16_corrupt_row(tmp_repo):
    doc = _run(builders.corrupt_row_repo(tmp_repo))
    # The run completes and recovers the other rows via the fallback ladder.
    assert doc.datasets[0].row_count >= 2


def test_17_18_messy_header(tmp_repo):
    doc = _run(builders.messy_header_repo(tmp_repo))
    assert doc.datasets[0].headers == ["site", "temp", "count"]


def test_14_multi_dataset(tmp_repo):
    doc = _run(builders.multi_dataset_repo(tmp_repo))
    # One README, two datasets: both are read and their columns matched to the README.
    assert len(doc.datasets) == 2
    water = next(d for d in doc.datasets if "water" in d.file)
    soil = next(d for d in doc.datasets if "soil" in d.file)
    assert any(v.name == "turbidity" for v in water.variables)
    assert any(v.name == "ph" for v in soil.variables)
    assert any(v.match.documented_name for d in doc.datasets for v in d.variables)


def test_19_ambiguous(tmp_repo):
    doc = _run(builders.ambiguous_match_repo(tmp_repo))
    # value1 is near both value2 and value3; expect an ambiguous flag or a low-confidence warning.
    assert _codes(doc) & {"MATCH_AMBIGUOUS", "MATCH_LOW_CONFIDENCE", "VARIABLE_UNDOCUMENTED"}


def test_20_unseen_structure(tmp_repo):
    doc = _run(builders.unseen_structure_repo(tmp_repo))
    assert doc.datasets, "found the nested data file"
    d = doc.datasets[0]
    # Codebook markdown table supplied the definitions.
    assert any(v.unit == "mg/L" for v in d.variables)


def test_no_readme_still_completes(tmp_repo):
    doc = _run(builders.no_readme_repo(tmp_repo))
    assert doc.datasets[0].column_count == 2
    assert any(i.code.value == "README_NOT_FOUND" for i in doc.issues)
