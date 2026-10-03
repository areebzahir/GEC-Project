"""Dialect sniffing and header detection on tricky inputs (DESIGN.md section 12)."""

from __future__ import annotations

from research_normalizer.config import PipelineConfig
from research_normalizer.tabular.dialect import sniff_dialect
from research_normalizer.tabular.header_detection import clean_headers, detect_header

CFG = PipelineConfig()


def _grid(text: str, sep: str = ","):
    return [line.split(sep) for line in text.strip().splitlines()]


def test_comma_detected():
    d = sniff_dialect("a,b,c\n1,2,3\n4,5,6\n", ".csv", CFG)
    assert d.delimiter == "," and d.field_count == 3


def test_semicolon_beats_comma_on_decimal_comma_file():
    # Data rows contain decimal commas; only ';' makes every row consistent.
    sample = "site;depth;temp\nA;1,5;12,1\nB;2,0;11,8\nC;3,1;10,2\n"
    d = sniff_dialect(sample, ".csv", CFG)
    assert d.delimiter == ";"


def test_tab_detected():
    d = sniff_dialect("a\tb\tc\n1\t2\t3\n4\t5\t6\n", ".tab", CFG)
    assert d.delimiter == "\t"


def test_pipe_detected():
    d = sniff_dialect("x|y|z\n1|2|3\n4|5|6\n", ".txt", CFG)
    assert d.delimiter == "|"


def test_preamble_offset():
    sample = "note line one\nnote line two\n\nsite,depth,temp\nA,1,2\nB,3,4\nC,5,6\n"
    d = sniff_dialect(sample, ".csv", CFG)
    assert d.delimiter == "," and d.skip_rows >= 1


def test_header_after_blank_and_title():
    grid = _grid("Field Survey 2021\n\nsite,temp,count\nA,12.1,30\nB,11.8,0\nC,10.2,500\n")
    grid = [g for g in grid if g != [""]]
    choice = detect_header(grid, CFG)
    assert choice.row_index is not None
    assert grid[choice.row_index] == ["site", "temp", "count"]


def test_clean_headers_blanks_and_duplicates():
    cleaned, notes = clean_headers(["id", "value", "value", "", "id"])
    assert cleaned == ["id", "value", "value__2", "column_4", "id__2"]
    kinds = {k for k, _ in notes}
    assert "blank" in kinds and "duplicate" in kinds
