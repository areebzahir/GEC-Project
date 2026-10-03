"""Work out which data file (and worksheet) a piece of README text is talking about.

READMEs announce the file a block of definitions belongs to in many ways: a heading that names the
file ("## survey.csv", "DATA-SPECIFIC INFORMATION FOR: survey.csv"), a key ("File: survey.csv",
"Dataset: plots", "Applies to: a.csv"), or a short first line under a heading. Worksheets are named
as "Sheet: X", "sheet 'X'", "(sheet X)", "book.xlsx – X" or "X tab of book.xlsx".

Functions here only read one piece of text and return a :class:`ScopeHint`; the running context
(which hint is in force where) lives in ``parser.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A data filename: common tabular/documentation extensions (no spaces, like most repository files).
FILENAME_RE = re.compile(r"([\w\-.]+\.(?:csv|tsv|tab|txt|xlsx|xls|xlsm|ods|dat|json|parquet|sav|dta))\b",
                         re.IGNORECASE)
_WORKBOOK_EXT = r"(?:xlsx|xls|xlsm|ods)"

# Keys whose value names the data file the following definitions describe. Longest first so that
# "file name" wins over "file". Keys must be followed by a separator, so "File list:" never matches.
_FILE_KEYS = (
    r"data[\s-]*specific\s+information\s+for", r"variables?\s+(?:for|in)", r"applies\s+to",
    r"data\s+file\s+name", r"data\s+file", r"file\s*name", r"file", r"source\s+file",
    r"complete\s+dataset", r"data\s*set\s+name", r"data\s*set", r"table\s+name", r"table",
)
_FILE_KEY_RE = re.compile(
    rf"^\s*(?:\d+[.)]\s*)?[*_`>#\s-]*(?:{'|'.join(_FILE_KEYS)})[*_`\s]*(?:[:=]|\s[-–]\s|\s+(?=\S+\.\w+))\s*(?P<value>.+)$",
    re.IGNORECASE,
)
# Keys whose value names a worksheet.
_SHEET_KEY_RE = re.compile(
    r"^\s*(?:\d+[.)]\s*)?[*_`>#\s-]*(?:work\s*sheet|sheet|tab)(?:\s+name)?[*_`\s]*[:=]\s*(?P<value>.+)$",
    re.IGNORECASE,
)
# Worksheet named inside a sentence or heading.
_SHEET_TEXT_RES = (
    # sheet 'Plots' / worksheet "Site info"
    re.compile(r"\b(?:work)?sheet\s+[\"'‘“](?P<sheet>[^\"'’”]+)[\"'’”]", re.IGNORECASE),
    # 'Plots' sheet / "Site info" worksheet / 'Plots' tab
    re.compile(r"[\"'‘“](?P<sheet>[^\"'’”]+)[\"'’”]\s+(?:work)?(?:sheet|tab)\b", re.IGNORECASE),
    # (sheet Plots) / (worksheet: Plots)
    re.compile(r"\(\s*(?:work)?sheet\s*:?\s*(?P<sheet>[^)]+)\)", re.IGNORECASE),
    # book.xlsx – Plots / book.xlsx / Plots / book.xlsx > Plots
    re.compile(rf"(?P<file>[\w\-.]+\.{_WORKBOOK_EXT})\s*(?:[–—/>›!]|\s-\s|::)\s*(?P<sheet>[^,;()\[\]]+)",
               re.IGNORECASE),
    # Plots tab of book.xlsx / "Site info" sheet in book.xlsx
    re.compile(rf"(?:[\"'‘“](?P<qsheet>[^\"'’”]+)[\"'’”]|(?P<sheet>[\w.\-]+))\s+(?:work)?(?:sheet|tab)\s+"
               rf"(?:of|in|from)\s+(?:the\s+)?(?:workbook\s+|file\s+)?(?P<file>[\w\-.]+\.{_WORKBOOK_EXT})",
               re.IGNORECASE),
)
# Values that are not a file at all ("Dataset: N/A").
_EMPTY_VALUES = {"", "n/a", "na", "none", "no", "not applicable", "tbd", "-"}
# Words that cannot be a sheet name on their own (they are the surrounding sentence).
_NOT_SHEET_WORDS = {"the", "this", "each", "every", "a", "an", "that", "which", "first", "second", "same"}


@dataclass(frozen=True, slots=True)
class ScopeHint:
    """A file and/or sheet named by one piece of text. Either may be None."""

    file: str | None = None
    sheet: str | None = None

    def __bool__(self) -> bool:
        return self.file is not None or self.sheet is not None


def data_filenames(text: str) -> list[str]:
    """Data filenames mentioned in ``text`` (README/documentation files themselves excluded)."""
    names = [m for m in FILENAME_RE.findall(text) if not is_documentation_name(m)]
    return list(dict.fromkeys(names))


def is_documentation_name(name: str) -> bool:
    # A README is never the data file a section describes (e.g. "ReadMe file: x_ReadMe.txt").
    return "readme" in name.lower().replace("_", "").replace("-", "")


def hint_from_heading(title: str | None) -> ScopeHint:
    """File/sheet named in a heading ("## survey.csv", "Plots (sheet Plots)")."""
    if not title:
        return ScopeHint()
    keyed = hint_from_key_line(title)
    if keyed:
        return keyed
    files = data_filenames(title)
    sheet_hint = _sheet_from_text(title)
    return ScopeHint(file=sheet_hint.file or (files[0] if files else None), sheet=sheet_hint.sheet)


def hint_from_key_line(text: str) -> ScopeHint:
    """File/sheet stated by a key line: "File: a.csv", "Dataset: plots", "Sheet: Plots"."""
    m = _SHEET_KEY_RE.match(text)
    if m:
        sheet = _clean_sheet(m.group("value"))
        return ScopeHint(sheet=sheet) if sheet else ScopeHint()
    m = _FILE_KEY_RE.match(text)
    if not m:
        return ScopeHint()
    value = m.group("value").strip()
    sheet_hint = _sheet_from_text(value)
    if sheet_hint.file:
        return sheet_hint
    files = data_filenames(value)
    if files:
        return ScopeHint(file=files[0], sheet=sheet_hint.sheet)
    # A bare identifier ("Dataset: plots") names a file by stem; a sentence or title does not.
    bare = value.strip("*_`'\"“”‘’ .:")
    if bare.lower() not in _EMPTY_VALUES and re.fullmatch(r"[A-Za-z][\w\-]{0,59}", bare):
        return ScopeHint(file=bare, sheet=sheet_hint.sheet)
    return ScopeHint()


def hint_from_lead_line(text: str) -> ScopeHint:
    """File/sheet named by a short line that introduces a block ("Variables in survey.csv:")."""
    keyed = hint_from_key_line(text)
    if keyed:
        return keyed
    # Only short lines: a long sentence that happens to mention a file is not a scope marker.
    if len(text.split()) > 12:
        return ScopeHint()
    sheet_hint = _sheet_from_text(text)
    files = data_filenames(text)
    return ScopeHint(file=sheet_hint.file or (files[0] if files else None), sheet=sheet_hint.sheet)


def _sheet_from_text(text: str) -> ScopeHint:
    for pattern in _SHEET_TEXT_RES:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groupdict()
        sheet = _clean_sheet(groups.get("qsheet") or groups.get("sheet") or "")
        if sheet:
            return ScopeHint(file=groups.get("file"), sheet=sheet)
    return ScopeHint()


def _clean_sheet(value: str) -> str | None:
    sheet = value.strip().strip("*_`'\"“”‘’").strip().rstrip(":.").strip()
    # Sheet names are short labels (Excel caps them at 31 characters); longer text is a sentence.
    if not sheet or len(sheet) > 31 or len(sheet.split()) > 4:
        return None
    if sheet.lower() in _NOT_SHEET_WORDS or sheet.lower() in _EMPTY_VALUES:
        return None
    return sheet
