"""Generic ingestion behaviour: content classification, codebooks, notes sheets, units, missing values.

Every input is a small synthetic file built in ``tmp_path`` so the tests say exactly what shape of
data each rule is about (DESIGN.md sections 10, 12, 13).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import xlsxwriter

from research_normalizer.config import DEFAULT_MISSING_VALUES, PipelineConfig
from research_normalizer.discovery import FileRole, discover
from research_normalizer.issues import IssueCollector
from research_normalizer.schema.document import ColumnType
from research_normalizer.tabular.base import read_table
from research_normalizer.tabular.classify import (
    looks_like_data_dictionary,
    looks_like_notes_sheet,
    sniff_text_role,
)
from research_normalizer.tabular.header_detection import is_units_row, unit_from_header
from research_normalizer.tabular.type_inference import infer_column_type
from research_normalizer.text_decoding import looks_like_text

CFG = PipelineConfig()

KEY_VALUE_README = (
    "GENERAL INFORMATION\n"
    "Title: Soil respiration under three grazing regimes\n"
    "Name: A. Researcher\n"
    "Institution: Example University, Department of Soil Science, Building 4\n"
    "Address: 12, Example Road, Sometown, Country\n"
    "Email: a.researcher@example.org\n"
    "Name: B. Researcher\n"
    "Institution: Another Institute, Field Station, North Campus\n"
    "\n"
    "The data were collected weekly, at dawn, over two growing seasons, using a portable analyser.\n"
    "Samples were stored at 4 degrees, then shipped, then analysed within two days of collection.\n"
)


def _roles(root: Path) -> dict[str, FileRole]:
    with discover(root, CFG, IssueCollector()) as result:
        return {f.relative_path: f.role for f in result.files}


def _read_one(root: Path, name: str, config: PipelineConfig = CFG):
    with discover(root, config, IssueCollector()) as result:
        file = next(f for f in result.files if f.relative_path == name)
        return read_table(file, config)


def _infer(values: list[str], extra: set[str] | None = None):
    return infer_column_type(pl.Series("c", values), "c", "f.csv", extra or set(), CFG)


# ------------------------------------------------------------------ task 1: discovery by content
def test_tab_delimited_txt_is_tabular(tmp_repo: Path):
    (tmp_repo / "measurements.txt").write_text(
        "plot\tdepth\tmoisture\nP1\t5\t0.21\nP2\t10\t0.25\nP3\t15\t0.30\nP4\t20\t0.31\n"
    )
    assert _roles(tmp_repo)["measurements.txt"] is FileRole.TABULAR


def test_prose_csv_is_documentation(tmp_repo: Path):
    (tmp_repo / "notes.csv").write_text(
        "These notes describe how the field campaign was organised and who took part.\n"
        "Each site was visited twice, once in spring and once in autumn, by two observers.\n"
        "Readings taken during rain were discarded, because the sensor drifts when wet.\n"
        "Contact the authors for the raw logger files.\n"
    )
    assert _roles(tmp_repo)["notes.csv"] is FileRole.DOCUMENTATION


def test_key_value_readme_with_commas_stays_documentation(tmp_repo: Path):
    (tmp_repo / "README.txt").write_text(KEY_VALUE_README)
    assert _roles(tmp_repo)["README.txt"] is FileRole.DOCUMENTATION


def test_tiny_table_uses_name_as_tie_breaker(tmp_repo: Path):
    # Header + one row is too little evidence on its own; the name decides.
    (tmp_repo / "values.csv").write_text("a,b\n1,2\n")
    (tmp_repo / "README.txt").write_text("a,b\n1,2\n")
    roles = _roles(tmp_repo)
    assert roles["values.csv"] is FileRole.TABULAR
    assert roles["README.txt"] is FileRole.DOCUMENTATION


def test_extensionless_and_binary_files(tmp_repo: Path):
    (tmp_repo / "observations").write_text("id;count\n1;4\n2;7\n3;9\n4;1\n")
    (tmp_repo / "blob.bin").write_bytes(b"\x00\x01\x02\x03binary\x00\xff" * 20)
    (tmp_repo / "config.json").write_text('{"a": 1, "b": 2}\n')
    roles = _roles(tmp_repo)
    assert roles["observations"] is FileRole.TABULAR
    assert roles["blob.bin"] is FileRole.OTHER
    assert roles["config.json"] is FileRole.OTHER


def test_looks_like_text_handles_bom_and_binary():
    assert looks_like_text("a,b\n1,2\n".encode("utf-16"))  # NUL bytes, but a BOM says text
    assert not looks_like_text(b"PK\x00\x00\x07\x08" * 10)
    assert looks_like_text("caf\u00e9;1\n".encode("cp1252"))


# ------------------------------------------------------------------ task 2: sniff_text_role
def test_sniff_readme_with_many_name_lines_is_documentation():
    text = "".join(f"Name: Person {i}\nORCID: 0000-0000-0000-000{i}\n" for i in range(10))
    assert sniff_text_role(text) == "documentation"


def test_sniff_csv_with_long_notes_column_is_tabular():
    rows = "\n".join(
        f"S{i},{i * 1.5},{i * 2},\"Sample was collected late in the day after heavy rain, so treat with care\""
        for i in range(8)
    )
    assert sniff_text_role("sample,mass,count,notes\n" + rows + "\n") == "tabular"


def test_sniff_two_column_key_value_csv_is_tabular():
    text = "key,value\nsite,North ridge\nyear,2019\nobserver,Team B\ncrs,EPSG:4326\n"
    assert sniff_text_role(text) == "tabular"


def test_sniff_markdown_table_document_is_documentation():
    text = "Overview of files\nSee below.\n| file | rows |\n|---|---|\n| a.csv | 10 |\n| b.csv | 12 |\n"
    assert sniff_text_role(text) == "documentation"


def test_sniff_data_with_short_title_preamble_is_tabular():
    text = "Field survey 2021\nCollected by team A\n\nsite,temp,count\nA,12.1,30\nB,11.8,0\nC,10.2,5\n"
    assert sniff_text_role(text) == "tabular"


def test_sniff_readme_ending_in_variable_list_is_documentation():
    head = KEY_VALUE_README * 2
    variables = "".join(f"var_{i}, description of variable {i}, mg/L\n" for i in range(30))
    assert sniff_text_role(head + "Variable list:\n" + variables) == "documentation"


# ------------------------------------------------------------------ task 3: data dictionaries
def test_looks_like_data_dictionary_positive_and_negative():
    rows = [["soil_ph", "Soil pH in water", ""], ["moist", "Volumetric moisture", "%"]]
    assert looks_like_data_dictionary(["Variable Name", "Description", "Units"], rows)
    # Ordinary data table: no name/description vocabulary.
    assert not looks_like_data_dictionary(["site", "ph", "moisture"], [["A", "6.1", "21"]])
    # A numeric code -> label lookup table is not a variable codebook.
    assert not looks_like_data_dictionary(["code", "label"], [["1", "Male"], ["2", "Female"]])
    # Weak name header ("name") with only a weak describer needs extra codebook evidence.
    assert not looks_like_data_dictionary(["name", "notes"], [["Alice", "late"], ["Bob", "ok"]])


def test_codebook_csv_is_read_as_dictionary(tmp_repo: Path):
    (tmp_repo / "codebook.csv").write_text(
        "variable,description,unit\n"
        "plot_id,Unique identifier of the plot,\n"
        "biomass,Above-ground dry biomass per square metre,g/m2\n"
        "height,Mean canopy height,cm\n"
    )
    assert _roles(tmp_repo)["codebook.csv"] is FileRole.TABULAR
    (table,) = _read_one(tmp_repo, "codebook.csv")
    assert table.kind == "dictionary"
    assert table.headers == ["variable", "description", "unit"]


def test_plain_data_csv_stays_data(tmp_repo: Path):
    (tmp_repo / "data.csv").write_text("site,ph,moisture\nA,6.1,21\nB,6.4,25\nC,5.9,19\n")
    (table,) = _read_one(tmp_repo, "data.csv")
    assert table.kind == "data"
    assert table.header_units == {}


# ------------------------------------------------------------------ task 4: notes sheets
def test_looks_like_notes_sheet():
    notes = [
        ["About this workbook"],
        ["This workbook contains the field measurements collected in the summer survey."],
        [""],
        ["Values were checked by two people before entry and outliers were flagged."],
    ]
    assert looks_like_notes_sheet(notes)
    data = [["site", "ph", "moisture"], ["A", "6.1", "21"], ["B", "6.4", "25"], ["C", "5.9", "19"]]
    assert not looks_like_notes_sheet(data)
    # A single column of short codes is a list of data values, not notes.
    assert not looks_like_notes_sheet([["species"], ["abc"], ["def"], ["ghi"]])


# ------------------------------------------------------------------ tasks 6 + 7: units and Excel
def test_delimited_units_row_is_removed_and_recorded(tmp_repo: Path):
    (tmp_repo / "growth.csv").write_text(
        "plant,height,mass,temp\n,(cm),(g),°C\nP1,12.5,3.1,21\nP2,14.0,3.4,22\nP3,11.2,2.9,20\n"
    )
    (table,) = _read_one(tmp_repo, "growth.csv")
    assert table.header_units == {"height": "cm", "mass": "g", "temp": "°C"}
    assert table.frame.height == 3
    assert table.frame.get_column("plant").to_list()[0] == "P1"


def test_unit_embedded_in_header_name(tmp_repo: Path):
    (tmp_repo / "obs.csv").write_text(
        "Site (north),Temperature (°C),Flux [mg/L]\nA,12.1,0.3\nB,11.8,0.4\nC,10.2,0.5\n"
    )
    (table,) = _read_one(tmp_repo, "obs.csv")
    assert table.headers == ["Site (north)", "Temperature (°C)", "Flux [mg/L]"]  # names unchanged
    assert table.header_units == {"Temperature (°C)": "°C", "Flux [mg/L]": "mg/L"}
    assert unit_from_header("Depth (years)") == "years"
    assert unit_from_header("Depth") is None


def test_is_units_row_rejects_data_rows():
    assert is_units_row(["", "(kg)", "mg/L", "%"])
    assert is_units_row(["-", "°C", "cm"])
    assert not is_units_row(["A1", "T2", "B3"])            # sample codes
    assert not is_units_row(["2020/01/02", "2020/02/03"])  # dates contain "/" but are data
    assert not is_units_row(["P1", "12.5", "kg"])          # any number => data
    assert not is_units_row(["m", "f"])                    # single letters are category codes


def _build_workbook(path: Path) -> None:
    wb = xlsxwriter.Workbook(str(path))
    data = wb.add_worksheet("Field data")
    data.write_row(0, 0, ["Summer field survey"])        # title row above the header
    data.write_row(2, 0, ["site", "depth", "temp"])
    data.write_row(3, 0, ["", "(m)", "°C"])               # units row
    for i, row in enumerate([["A", 1.5, 12.1], ["B", 2.0, 11.8], ["C", 3.1, 10.2]]):
        data.write_row(4 + i, 0, row)
    hidden = wb.add_worksheet("Backup")
    hidden.hide()
    hidden.write_row(0, 0, ["x", "y"])
    hidden.write_row(1, 0, [1, 2])
    hidden.write_row(2, 0, [3, 4])
    codebook = wb.add_worksheet("Variables")
    codebook.write_row(0, 0, ["Field", "Definition", "Units"])
    codebook.write_row(1, 0, ["site", "Sampling site code", ""])
    codebook.write_row(2, 0, ["depth", "Sampling depth below surface", "m"])
    codebook.write_row(3, 0, ["temp", "Water temperature", "°C"])
    notes = wb.add_worksheet("About")
    notes.write(0, 0, "About this workbook")
    notes.write(1, 0, "The survey was run by volunteers, who measured each site at noon.")
    notes.write(2, 0, "Depth is measured from the water surface to the probe tip.")
    notes.write(3, 0, "Questions about the data should go to the project coordinator.")
    wb.add_worksheet("Empty")
    wb.close()


def test_excel_sheet_kinds_units_hidden_and_title_rows(tmp_repo: Path):
    _build_workbook(tmp_repo / "survey.xlsx")
    tables = {t.worksheet_name: t for t in _read_one(tmp_repo, "survey.xlsx")}
    assert set(tables) == {"Field data", "Backup", "Variables", "About", "Empty"}

    field = tables["Field data"]
    assert field.kind == "data"
    assert field.headers == ["site", "depth", "temp"]
    assert field.structure.header_row == 3            # title row skipped
    assert field.header_units == {"depth": "m", "temp": "°C"}
    assert field.frame.get_column("site").to_list() == ["A", "B", "C"]

    backup = tables["Backup"]
    assert backup.kind == "data" and backup.frame.height == 2
    assert any(i.code.value == "EXCEL_HIDDEN_SHEET" for i in backup.issues)

    assert tables["Variables"].kind == "dictionary"

    about = tables["About"]
    assert about.kind == "notes"
    assert about.headers == ["text"]
    assert about.frame.get_column("text").to_list()[0] == "About this workbook"

    assert tables["Empty"].frame.width == 0
    assert any(i.code.value == "EXCEL_EMPTY_SHEET" for i in tables["Empty"].issues)


def test_hidden_sheet_skip_is_reported(tmp_repo: Path):
    _build_workbook(tmp_repo / "survey.xlsx")
    config = CFG.merged(process_hidden_sheets=False)
    backup = next(t for t in _read_one(tmp_repo, "survey.xlsx", config) if t.worksheet_name == "Backup")
    assert backup.frame.width == 0
    assert any(i.code.value == "EXCEL_HIDDEN_SHEET" for i in backup.issues)


# ------------------------------------------------------------------ task 5: missing values
def test_default_missing_tokens_are_conservative():
    for token in ("N.A.", "n.a.", "#N/A", "nan", "NAN", "missing", "Missing", "\u2014", "-", "#DIV/0!"):
        assert token in DEFAULT_MISSING_VALUES
    for risky in ("?", "0", "na", "Na"):
        assert risky not in DEFAULT_MISSING_VALUES


def test_numeric_typing_with_missing_tokens_reports_only_observed():
    r = _infer(["1.5", "N.A.", "2.5", "#N/A", "\u2014", "3.5", "", "#VALUE!"])
    assert r.type is ColumnType.NUMBER
    assert r.missing_values == sorted(["N.A.", "#N/A", "\u2014", "#VALUE!"])
    assert _infer(["1", "2", "3"]).missing_values == []


def test_declared_code_is_nulled_and_listed():
    r = _infer(["10", "-999", "12", "11", "13", "-999"], extra={"-999"})
    assert r.type is ColumnType.INTEGER
    assert r.missing_values == ["-999"]
    assert not any(i.code.value == "POSSIBLE_UNDECLARED_MISSING_CODE" for i in r.issues)


def test_undeclared_sentinel_outlier_warns_but_is_kept():
    r = _infer(["10.2", "11.5", "-999", "12.1", "10.9", "11.7", "-999"])
    assert r.type is ColumnType.NUMBER
    warnings = [i for i in r.issues if i.code.value == "POSSIBLE_UNDECLARED_MISSING_CODE"]
    assert len(warnings) == 1 and warnings[0].severity.value == "warning"
    assert "-999" in warnings[0].message
    assert r.missing_values == []  # never auto-nulled


def test_sentinel_inside_normal_range_is_not_flagged():
    # -9 is an ordinary value in a column spanning -20..20.
    r = _infer(["-20", "-9", "-5", "0", "4", "11", "20"])
    assert not any(i.code.value == "POSSIBLE_UNDECLARED_MISSING_CODE" for i in r.issues)


def test_csv_with_excel_errors_and_missing_tokens_types_numeric(tmp_repo: Path):
    (tmp_repo / "export.csv").write_text(
        "id,ratio\n1,0.5\n2,#DIV/0!\n3,0.7\n4,n.a.\n5,0.9\n"
    )
    (table,) = _read_one(tmp_repo, "export.csv")
    r = infer_column_type(table.frame.get_column("ratio"), "ratio", "export.csv", set(), CFG)
    assert r.type is ColumnType.NUMBER
    assert r.missing_values == ["#DIV/0!", "n.a."]
