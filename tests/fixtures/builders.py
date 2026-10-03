"""Code-generated synthetic repositories for the 20 competition test scenarios.

Fixtures are built in code (not committed as binary blobs) so a reviewer can see exactly what each
scenario contains (DESIGN.md section 21). Each builder writes files into a directory and returns it.
"""

from __future__ import annotations

from pathlib import Path

import xlsxwriter


def _write(path: Path, text: str, encoding: str = "utf-8", newline: str = "\n") -> None:
    data = text.replace("\n", newline).encode(encoding)
    path.write_bytes(data)
   


# 1. Clean CSV + clean README: every variable matches exactly.
def clean_repo(root: Path) -> Path:
    _write(root / "survey.csv", "id,age,score\n1,34,8.5\n2,41,7.2\n3,29,9.1\n")
    _write(root / "README.txt",
           "Title of dataset: Clean Survey\n"
           "Description of the dataset: A tidy example.\n"
           "DATA-SPECIFIC INFORMATION FOR: survey.csv\n"
           "Number of variables: 3\n"
           "Number of cases/rows: 3\n"
           "Variable List:\n"
           "id, unique respondent identifier\n"
           "age, respondent age, years\n"
           "score, test score\n")
    return root


# 2. TSV / .tab with Dataverse-style quoting.
def tsv_repo(root: Path) -> Path:
    _write(root / "data.tab", 'site\tdepth_m\ttemp_c\n"A"\t1.5\t12.1\n"B"\t2.0\t11.8\n')
    _write(root / "README.txt",
           "Title of dataset: TSV example\n"
           "DATA-SPECIFIC INFORMATION FOR: data.tab\n"
           "Variable List:\n"
           "site, sampling site\n"
           "depth_m, water depth, m\n"
           "temp_c, temperature, C\n")
    return root


# 3. Multi-sheet Excel including hidden, empty, and a dictionary sheet.
def excel_repo(root: Path) -> Path:
    path = root / "workbook.xlsx"
    wb = xlsxwriter.Workbook(str(path))
    data = wb.add_worksheet("Measurements")
    data.write_row(0, 0, ["site", "temp_c", "count"])
    for i, row in enumerate([["A", 12.1, 30], ["B", 11.8, 0], ["C", 10.2, 500]]):
        data.write_row(i + 1, 0, row)
    hidden = wb.add_worksheet("Internal")
    hidden.hide()
    hidden.write_row(0, 0, ["x", "y"])
    hidden.write_row(1, 0, [1, 2])
    wb.add_worksheet("Blank")
    wb.close()
    _write(root / "README.md",
           "# Title of dataset: Excel example\n\n"
           "## Variable List\n"
           "site, sampling site\n"
           "temp_c, temperature, C\n"
           "count, number of observations\n")
    return root


# 4. README names differ slightly from columns (abbreviations / case).
def fuzzy_names_repo(root: Path) -> Path:
    _write(root / "d.csv", "AnnualIncome,bodyweight_chg,HP_GreenFeed\n1000,0.5,33.4\n2000,0.7,32.2\n")
    _write(root / "README.txt",
           "Title of dataset: Fuzzy\n"
           "DATA-SPECIFIC INFORMATION FOR: d.csv\n"
           "Variable List:\n"
           "annual_income, annual income, CAD\n"
           "bodyweight_change, change in body weight, kg\n"
           "HP_Greenfeed, heat production, Mcal/d\n")
    return root


# 5. Duplicate columns.
def duplicate_columns_repo(root: Path) -> Path:
    _write(root / "dup.csv", "id,value,value,id\n1,2,3,4\n5,6,7,8\n")
    _write(root / "README.txt", "Title of dataset: Dup\nVariable List:\nid, identifier\nvalue, a value\n")
    return root


# 6. A column with no README description.
def undocumented_column_repo(root: Path) -> Path:
    _write(root / "u.csv", "id,secret\n1,x\n2,y\n")
    _write(root / "README.txt", "Title of dataset: Undoc\nDATA-SPECIFIC INFORMATION FOR: u.csv\nVariable List:\nid, identifier\n")
    return root


# 8. README variable missing from the data.
def phantom_variable_repo(root: Path) -> Path:
    _write(root / "p.csv", "id,age\n1,20\n2,30\n")
    _write(root / "README.txt",
           "Title of dataset: Phantom\nDATA-SPECIFIC INFORMATION FOR: p.csv\n"
           "Variable List:\nid, identifier\nage, age in years\nghost, a variable not in the data\n")
    return root

# 11. Non-UTF-8 encodings: accented values make this genuinely cp1252 (not plain ASCII).
def encoding_repo(root: Path) -> Path:
    _write(root / "fr.csv", "region,remarque\nQuébec,élevé\nMontréal,très faible\n", encoding="cp1252")
    _write(root / "README.txt", "Title of dataset: Encodage\nVariable List:\nregion, region name\nremarque, note\n",
           encoding="cp1252")
    return root


# 12. Wrong extension: a .csv that is actually semicolon-delimited.
def wrong_delimiter_repo(root: Path) -> Path:
    _write(root / "semi.csv", "site;depth;temp\nA;1.5;12\nB;2.0;11\nC;3.1;10\n")
    _write(root / "README.txt", "Title of dataset: Semi\nVariable List:\nsite, site\ndepth, depth\ntemp, temperature\n")
    return root


# 17/18. Blank rows, a title row and a units row before the header.
def messy_header_repo(root: Path) -> Path:
    _write(root / "m.csv",
           "Field Survey 2021\n"            # title row
           "\n"                              # blank
           "site,temp,count\n"              # header
           "A,12.1,30\n"
           "B,11.8,0\n"
           "C,10.2,500\n")
    _write(root / "README.txt", "Title of dataset: Messy\nVariable List:\nsite, site\ntemp, temperature\ncount, count\n")
    return root


# 16. A corrupted row / unbalanced quote.
def corrupt_row_repo(root: Path) -> Path:
    _write(root / "c.csv", 'a,b,c\n1,"oops,3\n4,5,6\n7,8,9\n')
    _write(root / "README.txt", "Title of dataset: Corrupt\nVariable List:\na, first\nb, second\nc, third\n")
    return root


# 14. One README describing multiple datasets.
def multi_dataset_repo(root: Path) -> Path:
    _write(root / "soil.csv", "site,ph\nA,6.5\nB,7.1\n")
    _write(root / "water.csv", "site,turbidity\nA,1.2\nB,0.8\n")
    _write(root / "README.txt",
           "Title of dataset: Two datasets\n"
           "DATA-SPECIFIC INFORMATION FOR: soil.csv\n"
           "Variable List:\nsite, site id\nph, soil pH\n"
           "DATA-SPECIFIC INFORMATION FOR: water.csv\n"
           "Variable List:\nsite, site id\nturbidity, water turbidity, NTU\n")
    return root


# 20. Unseen structure: nested folders, a codebook table, no obvious README name.
def unseen_structure_repo(root: Path) -> Path:
    (root / "data").mkdir()
    (root / "docs").mkdir()
    _write(root / "data" / "obs.csv", "stn,val\n1,10\n2,20\n")
    _write(root / "docs" / "codebook.md",
           "# Codebook\n\n"
           "DATA-SPECIFIC INFORMATION FOR: obs.csv\n\n"
           "| variable | description | unit |\n"
           "| --- | --- | --- |\n"
           "| stn | station number |  |\n"
           "| val | measured value | mg/L |\n")
    return root


# 19. An ambiguous fuzzy match: two documented names almost equally close to one column.
def ambiguous_match_repo(root: Path) -> Path:
    _write(root / "a.csv", "value1,other\n1,2\n3,4\n")
    _write(root / "README.txt",
           "Title of dataset: Ambiguous\nDATA-SPECIFIC INFORMATION FOR: a.csv\n"
           "Variable List:\nvalue2, a value\nvalue3, another value\nother, something else\n")
    return root


# A repository with no documentation at all.
def no_readme_repo(root: Path) -> Path:
    _write(root / "lonely.csv", "a,b\n1,2\n3,4\n")
    return root
