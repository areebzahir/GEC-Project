"""Recognise variable definitions in a README section, whatever the layout.

Repositories describe variables in very different ways. We run several independent recognisers over a
section and keep the one that explains the most lines (DESIGN.md section 11). Each returns a list of
:class:`VariableDefinition` with line-range provenance. Recognisers implemented:

1. block          - ``Name: X`` then ``Description:``/``Units:``/``Type:``/``Value labels:``/``Notes:``
2. table          - a Markdown pipe table whose header names the columns
3. aligned_table  - a whitespace/tab aligned table whose header names the columns (PDF text)
4. delimited_line - one variable per line: ``x, description, unit`` / ``x - description (unit)`` /
                    ``x: description [unit]`` / ``x = description`` / ``- `x`: description`` /
                    ``* **x** – description`` / ``x<TAB>description<TAB>unit``
5. anchored       - any line whose first token matches a known dataset column (last-resort fallback)

Afterwards :func:`mark_absent` flags variables the README says are intentionally not in the data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schema.document import ValueLabel
from .absence import absence_reason, is_exclusion_title
from .missing_codes import codes_from_text, is_missing_key, is_missing_label, parse_code_list
from .scope import FILENAME_RE
from .segmenter import Block, BlockType, Section


@dataclass(slots=True)
class VariableDefinition:
    """A documented variable, with where in the README it was found."""

    name: str
    description: str | None = None
    unit: str | None = None
    value_labels: list[ValueLabel] = field(default_factory=list)
    notes: str | None = None
    position: int = 0                      # order within the README list
    line_start: int = 0
    line_end: int = 0
    method: str = ""                       # recogniser name, for provenance
    missing_codes: list[str] = field(default_factory=list)  # per-variable missing codes ("-99 = missing")
    absent: bool = False                   # README says this variable is intentionally not in the data
    absent_reason: str | None = None       # the README wording that said so, for provenance
    declared_type: str | None = None       # "Type:" / type column as written ("integer", "date")
    value_range: str | None = None         # "Range:" / range column as written ("0-100")


# --------------------------------------------------------------------------- header / key synonyms
# One table of synonyms serves block keys ("Units:"), table headers ("| Units |") and dictionary
# columns. Compared after lower-casing and turning punctuation into spaces (see normalize_label).
ROLE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": ("name", "variable", "variable name", "var", "var name", "varname", "field", "field name",
             "column", "column name", "attribute", "attribute name", "parameter", "item", "code",
             "variable code"),
    "description": ("description", "label", "variable label", "definition", "meaning", "desc", "details",
                    "explanation", "question", "question text", "long name", "full name",
                    "variable description", "field description", "column description"),
    "unit": ("unit", "units", "unit of measure", "unit of measurement", "units of measure",
             "units of measurement", "measurement unit", "measurement units", "uom"),
    "type": ("type", "data type", "datatype", "variable type", "field type", "column type", "format",
             "storage type", "class"),
    "values": ("values", "value labels", "value label", "codes", "code list", "coding", "categories",
               "levels", "allowed values", "valid values", "permitted values", "value codes",
               "response options", "options"),
    "range": ("range", "valid range", "value range", "allowed range", "min max"),
    "missing": ("missing", "missing values", "missing value", "missing codes", "missing code",
                "missing value code", "missing value codes", "missing data codes", "na values", "na codes"),
    "notes": ("notes", "note", "comments", "comment", "remarks"),
    "file": ("file", "filename", "file name", "data file", "table", "table name", "dataset", "data set",
             "dataset name", "source file"),
    "sheet": ("sheet", "worksheet", "sheet name", "tab", "tab name"),
}
# When a header only *contains* a synonym ("Variable description"), roles are tried in this order so
# the more specific word wins ("Missing values" -> missing, not values; "Unit of temp" -> unit).
_CONTAINS_ORDER = ("missing", "unit", "type", "sheet", "file", "values", "range", "notes", "description", "name")
# Roles besides "name" that make a table header credible as a variable table.
_DETAIL_ROLES = ("description", "unit", "type", "values")


def normalize_label(text: str) -> str:
    """'Unit(s) of measure:' -> 'unit of measure'; '**Variable_Name**' -> 'variable name'."""
    text = re.sub(r"\(s\)", "", text.lower())
    text = re.sub(r"^\s*\d+[.)]\s*", "", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def role_of_key(key: str) -> str | None:
    """Role of a block key or header cell by exact synonym ("Units" -> "unit"), else None."""
    norm = normalize_label(key)
    for role, synonyms in ROLE_SYNONYMS.items():
        if norm in synonyms:
            return role
    return None


def classify_header(cells: list[str]) -> dict[str, int]:
    """Map roles to column indexes for a header row (exact synonyms first, then contained words)."""
    roles: dict[str, int] = {}
    norms = [normalize_label(c) for c in cells]
    for i, norm in enumerate(norms):
        role = role_of_key(norm)
        if role and role not in roles:
            roles[role] = i
    for i, norm in enumerate(norms):
        if i in roles.values() or not norm:
            continue
        words = f" {norm} "
        for role in _CONTAINS_ORDER:
            if role not in roles and any(f" {syn} " in words for syn in ROLE_SYNONYMS[role] if len(syn) > 3):
                roles[role] = i
                break
    return roles


def is_variable_header(roles: dict[str, int]) -> bool:
    return "name" in roles and any(r in roles for r in _DETAIL_ROLES)


# --------------------------------------------------------------------------- entry point
def recognise_variables(
    section: Section,
    documented_columns: frozenset[str] = frozenset(),
) -> list[VariableDefinition]:
    """Return the best set of variable definitions found in a section."""
    candidates = [
        _block_layout(section),
        _table_layout(section),
        _aligned_table_layout(section),
        _line_layout(section),
    ]
    if documented_columns:
        candidates.append(_anchored_layout(section, documented_columns))
    # Keep the recogniser that produced the most definitions (ties: earliest in the list, which is
    # also the most explicit layout).
    best = max(candidates, key=len)
    # "Excluded variables: a, b" lists are extra definitions, whatever layout the rest uses.
    known = {d.name for d in best}
    best = best + [d for d in _inline_exclusion_list(section) if d.name not in known]
    best.sort(key=lambda d: d.line_start)
    for position, definition in enumerate(best, start=1):
        definition.position = position
    return best


def line_coverage(section: Section, definitions: list[VariableDefinition]) -> float:
    """Share of the section's content lines that became definitions (1.0 = a pure variable list)."""
    content = [b for b in section.blocks if b.type in (BlockType.TEXT, BlockType.LIST_ITEM, BlockType.KEY_VALUE)
               and not _is_reserved_key(b)]
    if not content:
        return 0.0
    covered = {b.line_start for b in content
               if any(d.line_start <= b.line_start <= d.line_end for d in definitions)}
    return len(covered) / len(content)


# --------------------------------------------------------------------------- recogniser 1: block
# Keys that start a new variable block. Narrower than the "name" header role: "Code:" or "Item:"
# inside a block are details of the current variable, not a new one.
_BLOCK_NAME_KEYS = {"name", "variable", "variable name", "var name", "field", "field name", "column",
                    "column name"}
# Cornell-template key that states missing codes for the whole file, not for one variable.
_FILE_LEVEL_MISSING_KEYS = {"missing data codes", "missing data code"}


def _block_layout(section: Section) -> list[VariableDefinition]:
    defs: list[VariableDefinition] = []
    current: VariableDefinition | None = None
    for block in section.blocks:
        if block.type is not BlockType.KEY_VALUE or block.key is None:
            continue
        role = role_of_key(block.key)
        key = normalize_label(block.key)
        value = (block.value or "").strip()
        if key in _BLOCK_NAME_KEYS:
            current = VariableDefinition(name=_strip_markup(value), line_start=block.line_start,
                                         line_end=block.line_end, method="block")
            defs.append(current)
            continue
        if current is None:
            continue
        if key in _FILE_LEVEL_MISSING_KEYS or (is_missing_key(block.key) and role != "missing"):
            # "Missing data codes:" is the file-level template key; it ends the current block.
            current = None
            continue
        if role is None or role in ("file", "sheet"):
            continue
        _apply_block_field(current, role, value)
        current.line_end = block.line_end
    for definition in defs:
        _finish_definition(definition)
    # A block recogniser is only credible if it saw explicit Name: keys with a value.
    return [d for d in defs if d.name and is_plausible_name(d.name)]


def _apply_block_field(definition: VariableDefinition, role: str, value: str) -> None:
    if not value:
        return
    if role == "description":
        definition.description = value
    elif role == "unit":
        definition.unit = value
    elif role == "type":
        definition.declared_type = value
    elif role == "range":
        definition.value_range = value
    elif role == "values":
        labels, missing = parse_value_labels(value)
        definition.value_labels = labels
        definition.missing_codes.extend(missing)
    elif role == "missing":
        definition.missing_codes.extend(codes_from_text(value) or parse_code_list(value))
    elif role == "notes":
        definition.notes = value


def _finish_definition(definition: VariableDefinition) -> None:
    """Shared clean-up: peel a trailing unit off the description, collect missing-code sentences."""
    if definition.description and definition.unit is None:
        definition.description, definition.unit = split_trailing_unit(definition.description)
    for text in (definition.description, definition.notes):
        found = codes_from_text(text) if text else None
        if found:
            definition.missing_codes.extend(found)
    definition.missing_codes = list(dict.fromkeys(definition.missing_codes))


# ------------------------------------------------------------------------- recogniser 2: pipe table
def _table_layout(section: Section) -> list[VariableDefinition]:
    rows = [b for b in section.blocks if b.type is BlockType.TABLE_ROW]
    defs: list[VariableDefinition] = []
    roles: dict[str, int] | None = None
    for row in rows:
        row_roles = classify_header(row.cells)
        if is_variable_header(row_roles) and _is_header_like(row.cells):
            roles = row_roles          # a (new) header row starts a (new) table
            continue
        if roles is not None:
            definition = definition_from_cells(row.cells, roles, row.line_start, "table")
            if definition is not None:
                defs.append(definition)
    return defs


def definition_from_cells(cells: list[str], roles: dict[str, int], line: int, method: str
                          ) -> VariableDefinition | None:
    """Build one definition from a table/dictionary row using the header's role -> column map."""
    def cell(role: str) -> str:
        i = roles.get(role)
        return cells[i].strip() if i is not None and i < len(cells) else ""

    name = _strip_markup(cell("name"))
    if not name or not is_plausible_name(name):
        return None
    definition = VariableDefinition(
        name=name, description=cell("description") or None, unit=cell("unit") or None,
        notes=cell("notes") or None, declared_type=cell("type") or None, value_range=cell("range") or None,
        line_start=line, line_end=line, method=method,
    )
    labels, missing = parse_value_labels(cell("values"))
    definition.value_labels = labels
    definition.missing_codes = missing + parse_code_list(cell("missing")) if cell("missing") else missing
    _finish_definition(definition)
    return definition


def _is_header_like(cells: list[str]) -> bool:
    # Header cells are short labels; a data row whose name happens to be "name" has a long description.
    return all(len(c.split()) <= 5 for c in cells)


# ------------------------------------------------------------------ recogniser 3: aligned table
_COLUMN_GAP_RE = re.compile(r"\t+|\s{2,}")


def _aligned_table_layout(section: Section) -> list[VariableDefinition]:
    defs: list[VariableDefinition] = []
    header: tuple[dict[str, int], list[int]] | None = None   # roles, column start offsets
    for block in section.blocks:
        if block.type not in (BlockType.TEXT, BlockType.LIST_ITEM, BlockType.KEY_VALUE):
            header = None
            continue
        text = block.text
        cells = _split_columns(text)
        roles = classify_header(cells)
        if len(cells) >= 2 and is_variable_header(roles) and _is_header_like(cells):
            header = (roles, [m.start() for m in re.finditer(r"[^\s]+(?: [^\s]+)*", text)])
            continue
        if header is None:
            continue
        if len(cells) < 2:
            header = None              # a line without columns ends the table
            continue
        roles, offsets = header
        if len(cells) != len(offsets):
            cells = _slice_columns(text, offsets)
        definition = definition_from_cells(cells, roles, block.line_start, "aligned_table")
        if definition is not None:
            defs.append(definition)
    return defs


def _split_columns(text: str) -> list[str]:
    return [c.strip() for c in _COLUMN_GAP_RE.split(text.strip()) if c.strip()]


def _slice_columns(text: str, offsets: list[int]) -> list[str]:
    """Cut a row at the header's column positions (rows with an empty cell split unevenly)."""
    bounds = offsets + [len(text) + 1]
    return [text[bounds[i]:bounds[i + 1]].strip() for i in range(len(offsets))]


# ------------------------------------------------------------------ recogniser 4: one line each
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*•·+]|\d+[.)])\s+")
# "**x** – description", "`x`: description", "__x__ = description"
_EMPHASIS_NAME_RE = re.compile(r"^(?P<q>\*\*|__|`|\*)(?P<name>[^*`\s][^*`]*?)(?P=q)\s*"
                               r"(?P<sep>[:=,]|[-–—]+)?\s*(?P<rest>.*)$")
# "x, description" / "x: description" / "x = description" / "x - description" / "x – description".
# The name is at most three words, which keeps ordinary sentences from looking like definitions.
_PLAIN_NAME_RE = re.compile(r"^(?P<name>[^\s,:=]+(?:\s+[^\s,:=]+){0,2}?)"
                            r"(?P<sep>\s*[,:=]|\s+[-–—]+\s+|\s*[–—]\s*)\s*(?P<rest>.+)$")
# A bare identifier as a list item ("- site_id"), used for plain lists of names.
_BARE_NAME_RE = re.compile(r"^[A-Za-z_][\w.\-]*$")
# Lines that are never variable definitions: links, e-mails, ORCIDs, citations ("Smith, J., 2020.").
_NOISE_RE = re.compile(
    r"https?://|www\.|\S+@\S+\.\w+|\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b"
    r"|^[A-Z][\w'’\-]+,\s+(?:[A-Z]\.\s*){1,3}(?:,|and|&|\(|\d{4}|$)"
    r"|^[A-Z][\w'’\-]+,\s+[A-Z][\w'’\-]+(?:;|,\s+[A-Z][\w'’\-]+;)"
    r"|\(\d{4}\)\.|\b(?:19|20)\d{2}[a-z]?\.\s+[A-Z]"
)
_MAX_LINE_CHARS = 200   # longer lines are prose paragraphs, not one-line definitions


def _line_layout(section: Section) -> list[VariableDefinition]:
    defs: list[VariableDefinition] = []
    bare: list[VariableDefinition] = []
    for block in section.blocks:
        if block.type not in (BlockType.LIST_ITEM, BlockType.TEXT, BlockType.KEY_VALUE):
            continue
        if _is_reserved_key(block):
            continue
        text = _block_line(block)
        parsed = parse_definition_line(text)
        if parsed is None:
            if block.type is BlockType.LIST_ITEM and _BARE_NAME_RE.match(text) and is_plausible_name(text) \
                    and not FILENAME_RE.fullmatch(text):
                bare.append(VariableDefinition(name=text, line_start=block.line_start,
                                               line_end=block.line_end, method="name_list"))
            continue
        name, description, unit = parsed
        definition = VariableDefinition(name=name, description=description, unit=unit,
                                        line_start=block.line_start, line_end=block.line_end,
                                        method="delimited_line")
        _finish_definition(definition)
        defs.append(definition)
    # A list of bare names only counts when there are several of them ("- id", "- age", "- score").
    if len(bare) >= 2:
        defs = sorted(defs + bare, key=lambda d: d.line_start)
    return defs


def _block_line(block: Block) -> str:
    """The definition text of a block (key/values keep their wrapped continuation)."""
    if block.type is BlockType.KEY_VALUE and block.key is not None:
        return f"{block.key.strip()}: {(block.value or '').strip()}".strip()
    return block.text.strip()


def parse_definition_line(line: str) -> tuple[str, str | None, str | None] | None:
    """Split one definition line into (name, description, unit), or None if it is not one."""
    text = _LIST_MARKER_RE.sub("", line.strip(), count=1).strip()
    if not text or len(text) > _MAX_LINE_CHARS or text.endswith(":") or _NOISE_RE.search(text):
        return None
    if "\t" in text:
        return _parse_tab_line(text)
    m = _EMPHASIS_NAME_RE.match(text) or _PLAIN_NAME_RE.match(text)
    if not m:
        return None
    name = m.group("name").strip().rstrip(":").strip()
    sep = (m.group("sep") or "").strip()
    rest = m.group("rest").strip()
    if not rest or not is_plausible_name(name) or _is_header_pair(name, rest) or _is_prose(rest):
        return None
    description, unit = _split_description_and_unit(rest, comma_style=sep == ",")
    return (name, description, unit) if description else None


def _parse_tab_line(text: str) -> tuple[str, str | None, str | None] | None:
    cells = [c.strip() for c in text.split("\t") if c.strip()]
    if len(cells) < 2 or not is_plausible_name(cells[0]) or _is_header_pair(cells[0], cells[1]):
        return None
    unit = cells[2] if len(cells) >= 3 else None
    return (_strip_markup(cells[0]), cells[1], unit)


def _split_description_and_unit(rest: str, comma_style: bool) -> tuple[str | None, str | None]:
    """'distance from carcass, cm' -> ('distance from carcass', 'cm'); 'depth (m)' -> ('depth', 'm')."""
    parts = _split_top_level_commas(rest)
    unit = None
    if len(parts) >= 2:
        last = parts[-1]
        # In "name, description, unit" lines the last slot is the unit by convention, so a short code
        # such as "AWCD" or "NTU" is accepted there; after other separators it must look like a unit.
        if looks_like_unit(last) or (comma_style and _is_unit_code(last)):
            unit = last
            parts = parts[:-1]
    description = ", ".join(parts).strip().rstrip(",").strip() or None
    if description and unit is None:
        description, unit = split_trailing_unit(description)
    return description, unit


def _split_top_level_commas(text: str) -> list[str]:
    """Split on commas that are not inside brackets ("substrate (D, L-Glycerol), AWCD")."""
    parts, depth, current = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    parts.append("".join(current).strip())
    return [p for p in parts if p]


def _is_header_pair(name: str, rest: str) -> bool:
    # "Variable, Description, Units" is a header line, not a variable called "Variable".
    return role_of_key(name) == "name" and role_of_key(_split_top_level_commas(rest)[0]) is not None


def _is_prose(text: str) -> bool:
    # Two or more sentences ("Soil was dried. Samples were...") means a paragraph, not a definition.
    return len(re.findall(r"[a-z0-9)][.!?]\s+[A-Z]", text)) >= 1 and len(text) > 80


# ----------------------------------------------------------------------------- units
# Unit words recognised on their own ("years", "kg", "°C"). Lower-case comparison.
_UNIT_WORDS = {
    "g", "kg", "mg", "ug", "µg", "μg", "ng", "m", "cm", "mm", "km", "um", "µm", "μm", "nm", "l", "ml", "ul",
    "µl", "s", "sec", "secs", "min", "mins", "h", "hr", "hrs", "hour", "hours", "d", "day", "days", "wk",
    "week", "weeks", "mo", "month", "months", "yr", "yrs", "year", "years", "c", "°c", "°f", "k", "pa", "kpa",
    "mpa", "hpa", "bar", "mbar", "w", "kw", "j", "kj", "mj", "cal", "kcal", "mcal", "hz", "khz", "mv", "ma",
    "ppm", "ppb", "ppt", "mol", "mmol", "umol", "µmol", "mm", "ha", "ntu", "psu", "db", "lux", "cells",
    "count", "counts", "percent", "degrees", "deg", "usd", "cad", "eur", "gbp", "$", "celsius",
    "fahrenheit", "kelvin", "meters", "metres", "meter", "metre", "grams", "kilograms", "liters", "litres",
    "seconds", "minutes", "iu", "au", "mmhg", "bpm", "kgs", "lbs", "lb", "oz", "ft", "in", "mi", "acres",
}
# Symbols / patterns that only occur in units: "kg/d", "%", "°C", "m^2", "µm", "mg L-1", "per".
_UNIT_SYMBOL_RE = re.compile(r"[/%°^µμ·]|\b[A-Za-z]+-[123]\b|\bper\b|^[A-Za-z]{1,3}[23]$")
# Slash pairs of category words ("yes/no", "true/false") are value lists, not units.
_CATEGORY_PAIR_RE = re.compile(r"^(?:yes|no|y|n|true|false|t|f|present|absent|and|or|male|female|m)"
                               r"\s*/\s*(?:yes|no|y|n|true|false|t|f|present|absent|and|or|male|female|f)$",
                               re.IGNORECASE)
_TRAILING_BRACKET_RE = re.compile(r"\s+[\(\[](?P<unit>[^()\[\]]{1,20})[\)\]]\s*$")


def looks_like_unit(text: str) -> bool:
    """True for short unit-like strings: 'cm', 'kg/d', '%', '°C', 'mg L-1', 'cells/gSoil'."""
    s = text.strip()
    if not s or len(s) > 20 or len(s.split()) > 3 or _CATEGORY_PAIR_RE.match(s):
        return False
    if _UNIT_SYMBOL_RE.search(s):
        return True
    return all(w.lower().strip(".") in _UNIT_WORDS for w in re.split(r"[\s*]+", s) if w)


def _is_unit_code(text: str) -> bool:
    # A short upper-case code in the unit slot: "AWCD", "NTU", "CAD", "C".
    return bool(re.fullmatch(r"[A-Z]{1,6}\d?", text.strip()))


def split_trailing_unit(description: str) -> tuple[str, str | None]:
    """'water depth (m)' -> ('water depth', 'm'); 'humic-like component (C1)' is left alone."""
    m = _TRAILING_BRACKET_RE.search(description)
    if m and looks_like_unit(m.group("unit")):
        return description[: m.start()].rstrip(" ,;"), m.group("unit").strip()
    return description, None


# ----------------------------------------------------------------------------- names
_NOT_NAMES = {"none", "n/a", "na", "tbd", "yes", "no", "note", "notes", "see", "e.g", "i.e", "etc"}


def is_plausible_name(name: str) -> bool:
    """Reject things that cannot be a variable name: sentences, numbers, URLs, filenames, people."""
    name = name.strip()
    if not name or len(name) > 60 or name.lower().strip(".") in _NOT_NAMES:
        return False
    if len(name.split()) > 3 or name.endswith(".") or "@" in name or "://" in name:
        return False
    if re.fullmatch(r"[\d.,\-\s]+", name):         # "1", "2.5": list numbers or value codes
        return False
    if FILENAME_RE.fullmatch(name):                 # "survey.csv" is a file, not a variable
        return False
    return bool(re.match(r"[\w\"'`(\[]", name))


def _strip_markup(name: str) -> str:
    return name.strip().strip("*_`").strip()


# ------------------------------------------------------------------- recogniser 5: column-anchored
def _anchored_layout(section: Section, columns: frozenset[str]) -> list[VariableDefinition]:
    """Last resort: any line whose first token matches a known column name."""
    from ..matching.normalize import normalize_name

    norm_cols = {normalize_name(c): c for c in columns}
    defs: list[VariableDefinition] = []
    for block in section.blocks:
        if block.type not in (BlockType.LIST_ITEM, BlockType.TEXT, BlockType.KEY_VALUE):
            continue
        text = _block_line(block)
        if not text or _is_reserved_key(block):
            continue
        first = re.split(r"[\s,:=\-\t]", text, maxsplit=1)
        token = first[0].strip().strip("*_`")
        if normalize_name(token) in norm_cols:
            rest = first[1].strip(" ,:=-\t") if len(first) > 1 else None
            defs.append(VariableDefinition(name=token, description=rest or None,
                                           line_start=block.line_start, line_end=block.line_end,
                                           method="anchored"))
    return defs


# --------------------------------------------------------------------------- reserved keys
# Key/value keys that describe the file or the README itself, never a variable called that.
_RESERVED_KEY_RE = re.compile(
    r"^(?:number of|missing|specialized|specialised|abbreviations?|files?|file ?name|file list|data file|"
    r"datasets?|data set|data specific|sheet|worksheet|tab|table|relationships?|variable list|"
    r"notes?|comments?|applies to|complete dataset|readme)\b"
)


def _is_reserved_key(block: Block) -> bool:
    if block.type is not BlockType.KEY_VALUE or block.key is None:
        return False
    if re.search(r"`|\*\*|__", block.key):
        return False       # "- `type`: ..." marks a variable name explicitly, even a reserved word
    if role_of_key(block.key) is not None or is_missing_key(block.key):
        return True
    return bool(_RESERVED_KEY_RE.match(normalize_label(block.key)))


# --------------------------------------------------------------------------- value labels
_VALUE_LABEL_RE = re.compile(r"^\s*[\"'“]?(?P<code>[\w.+-]+)[\"'”]?\s*(?:=|:|\s-\s|\s–\s)\s*(?P<label>.+)$")


def parse_value_labels(value: str) -> tuple[list[ValueLabel], list[str]]:
    """Parse '1 = Male; 2 = Female; -9 = missing' into labels and missing codes.

    Codes whose label means "missing" ("missing", "not recorded", "no data", "not applicable") are
    returned as missing codes instead of labels.
    """
    if not value or value.strip().lower() in {"n/a", "na", "none", "-"}:
        return [], []
    labels: list[ValueLabel] = []
    missing: list[str] = []
    for part in re.split(r"[;,\n]|\s\|\s", value):
        m = _VALUE_LABEL_RE.match(part.strip())
        if not m:
            continue
        code, label = m.group("code").strip(), m.group("label").strip()
        if is_missing_label(label):
            missing.append(code)
        else:
            labels.append(ValueLabel(code=code, label=label))
    return labels, missing


def _parse_value_labels(value: str) -> list[ValueLabel]:
    """Backward-compatible wrapper: value labels only."""
    return parse_value_labels(value)[0]


# --------------------------------------------------------------------------- absence
def mark_absent(definitions: list[VariableDefinition], section: Section) -> None:
    """Flag definitions the README says are intentionally not in the data (DESIGN.md section 11).

    Three signals: the section title ("Variables not included"), a lead-in line ending in ':' that
    says so ("The following variables were removed from the public release:"), or the variable's own
    description/notes ("withheld for privacy").
    """
    title_reason = section.title if is_exclusion_title(section.title) else None
    lead_in = _lead_in_spans(section)
    for definition in definitions:
        reason = (absence_reason(definition.description, definition.notes)
                  or title_reason
                  or next((text for start, end, text in lead_in if start < definition.line_start <= end), None))
        if reason:
            definition.absent = True
            definition.absent_reason = reason


def _lead_in_spans(section: Section) -> list[tuple[int, int, str]]:
    """(after_line, until_line, text) for each exclusion lead-in and the list items that follow it."""
    spans: list[tuple[int, int, str]] = []
    blocks = section.blocks
    for i, block in enumerate(blocks):
        text = _block_line(block)
        if not text.endswith(":") or not is_exclusion_title(text):
            continue
        end = block.line_end
        for follow in blocks[i + 1:]:
            if follow.type is not BlockType.LIST_ITEM:
                break
            end = follow.line_end
        spans.append((block.line_end, end, text.rstrip(":").strip()))
    return spans


def _inline_exclusion_list(section: Section) -> list[VariableDefinition]:
    """'Excluded variables: ssn, address' -> absent definitions for ssn and address."""
    defs = []
    for block in section.blocks:
        if block.type is not BlockType.KEY_VALUE or not block.key or not is_exclusion_title(block.key):
            continue
        if not re.search(r"\b(?:variables?|columns?|fields?)\b", block.key, re.IGNORECASE):
            continue
        value = (block.value or "").strip()
        for name in re.split(r"\s*(?:[,;]|\band\b)\s*", value):
            name = _strip_markup(name)
            if _BARE_NAME_RE.match(name) and is_plausible_name(name):
                defs.append(VariableDefinition(name=name, line_start=block.line_start, line_end=block.line_end,
                                               method="exclusion_list", absent=True,
                                               absent_reason=f"{block.key.strip()}: {value}"))
    return defs


# --------------------------------------------------------------------------- declared counts & codes
_COUNT_VAR_RE = re.compile(r"number of (?:variables|columns|fields)\s*[:=]?\s*(\d+)", re.IGNORECASE)
_COUNT_ROW_RE = re.compile(r"number of (?:cases|rows|observations|records)(?:/rows)?\s*[:=]?\s*(\d+)",
                           re.IGNORECASE)


def find_declared_counts(section: Section) -> tuple[int | None, int | None]:
    """Extract 'Number of variables' and 'Number of cases/rows' from a section."""
    text = "\n".join(_block_line(b) for b in section.blocks)
    var = _COUNT_VAR_RE.search(text)
    row = _COUNT_ROW_RE.search(text)
    return (int(var.group(1)) if var else None, int(row.group(1)) if row else None)
