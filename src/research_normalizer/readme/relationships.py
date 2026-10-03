"""Relationships between data files that the README states in words.

Two sources (DESIGN.md section 11):
  * a sentence naming two or more data files together with linking language
    ("plots.csv and trees.csv can be joined on `plot_id`");
  * a "Relationship between files: ..." key/value whose value is not just "No"/"None".
The sentence is kept verbatim, and linking variable names are pulled out when they are quoted or
follow words such as "on", "by", "via", "using", "key", "column".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .scope import FILENAME_RE, data_filenames
from .segmenter import BlockType, Section

# Words that signal two files are connected.
_LINK_CUE_RE = re.compile(
    r"\b(?:join(?:ed|s|ing)?|link(?:ed|s|ing)?|merg(?:e|ed|es|ing)|keys?|foreign\s+key|relat(?:e|ed|es|ion|ionship)|"
    r"combin(?:e|ed|es|ing)|match(?:ed|es|ing)?|common|shared|correspond(?:s|ing)?)\b",
    re.IGNORECASE,
)
# Key of a "Relationship between files" style key/value.
_RELATION_KEY_RE = re.compile(r"\brelationships?\b.*\b(?:files?|tables?|datasets?|sheets?)\b|\blinkage\b|\bjoin keys?\b",
                              re.IGNORECASE)
# Values that mean "no relationship".
_NO_RELATION = {"", "no", "none", "n/a", "na", "not applicable", "nil", "-", "tbd", "no relationship"}
# Quoted / backticked names: `site_id`, "site_id", 'site_id'.
_QUOTED_RE = re.compile(r"[`\"“‘'](?P<name>[A-Za-z_][\w.\-]{0,59})[`\"”’']")
_IDENT = r"[A-Za-z_][\w\-]*"
_NAME_LIST = rf"(?P<names>{_IDENT}(?:\s*(?:,|\band\b|&)\s*{_IDENT})*)"
_ARTICLE = r"(?:(?:the|a|an|common|shared)\s+)*"
_ROLE_WORD = r"(?:keys?|columns?|fields?|variables?)"
# "joined on site_id and visit_no" / "linked via plot_id": only identifier-looking names count here,
# because ordinary English follows these prepositions too ("combined using R", "linked by site").
_AFTER_PREPOSITION_RE = re.compile(rf"\b(?:on|by|via|using)\s+{_ARTICLE}{_NAME_LIST}", re.IGNORECASE)
# "key column plot_id" / "the site column" / "plot_id key": a role word makes any word a name.
_AFTER_ROLE_RE = re.compile(rf"\b{_ROLE_WORD}\s+{_NAME_LIST}", re.IGNORECASE)
_BEFORE_ROLE_RE = re.compile(rf"\b(?P<names>{_IDENT})\s+{_ROLE_WORD}\b", re.IGNORECASE)
# Words that sit next to "on"/"key"/"column" but are not variable names.
_STOPWORDS = {
    "the", "a", "an", "this", "that", "these", "those", "each", "both", "all", "and", "or", "of", "to", "in",
    "with", "its", "their", "file", "files", "dataset", "datasets", "data", "table", "tables", "common",
    "shared", "same", "unique", "primary", "foreign", "key", "keys", "column", "columns", "field", "fields",
    "variable", "variables", "can", "be", "is", "are", "was", "were", "which", "using", "via", "by", "on",
    "row", "rows", "record", "records", "value", "values", "name", "names", "it", "them", "one", "other",
    "another", "between", "per", "linking", "link", "join", "joining", "joined", "matching", "match",
    "merge", "merged", "merging", "identifying", "following", "as", "for", "is", "has", "have", "share",
}


@dataclass(slots=True)
class RelationshipStatement:
    """A README sentence that relates data files (converted to DocumentedRelationship by the parser)."""

    files: list[str]
    variables: list[str]
    text: str
    line_start: int
    line_end: int


def find_relationships(sections: list[Section]) -> list[RelationshipStatement]:
    """All explicit file relationships stated in the README, in document order."""
    found: list[RelationshipStatement] = []
    seen: set[tuple[int, str]] = set()
    for section in sections:
        for block in section.blocks:
            for statement in _statements_in_block(block):
                key = (statement.line_start, statement.text)
                if key not in seen:
                    seen.add(key)
                    found.append(statement)
    return found


def _statements_in_block(block) -> list[RelationshipStatement]:
    if block.type is BlockType.HEADING:
        return []
    # A "Relationship between files: ..." key/value counts whenever its value says something.
    if block.type is BlockType.KEY_VALUE and block.key and _RELATION_KEY_RE.search(block.key):
        value = (block.value or "").strip()
        if value.lower().strip(" .") in _NO_RELATION:
            return []
        text = f"{block.key.strip()}: {value}"
        return [RelationshipStatement(data_filenames(value), linking_variables(value), text,
                                      block.line_start, block.line_end)]
    text = f"{block.key}: {block.value}" if block.type is BlockType.KEY_VALUE and block.key else block.text
    out = []
    for sentence in _sentences(text):
        files = data_filenames(sentence)
        if len(files) >= 2 and _LINK_CUE_RE.search(FILENAME_RE.sub(" ", sentence)):
            out.append(RelationshipStatement(files, linking_variables(sentence), sentence.strip(),
                                             block.line_start, block.line_end))
    return out


def linking_variables(text: str) -> list[str]:
    """Variable names a relationship sentence says the files share or join on."""
    # Filenames are not variables; blank them out first so "a.csv" doesn't yield "a".
    clean = FILENAME_RE.sub(" ", text)
    names = [m.group("name") for m in _QUOTED_RE.finditer(clean)]
    for group in _name_groups(_AFTER_PREPOSITION_RE, clean):
        # "on site_id and date": one clear identifier vouches for the rest of the list.
        if any(_looks_like_identifier(n) for n in group):
            names.extend(group)
    for pattern in (_AFTER_ROLE_RE, _BEFORE_ROLE_RE):
        for group in _name_groups(pattern, clean):
            names.extend(group)
    return list(dict.fromkeys(names))


def _name_groups(pattern: re.Pattern[str], text: str) -> list[list[str]]:
    groups = []
    for m in pattern.finditer(text):
        parts = [p.strip() for p in re.split(r"\s*(?:,|\band\b|&)\s*", m.group("names"))]
        groups.append([p for p in parts if p and p.lower() not in _STOPWORDS])
    return groups


def _looks_like_identifier(name: str) -> bool:
    # snake_case, digits, camelCase or an acronym: clearly a column name rather than an English word.
    return bool(re.search(r"_|\d|[a-z][A-Z]", name)) or (name.isupper() and len(name) >= 2)


def _sentences(text: str) -> list[str]:
    # Split at sentence ends; a filename's dot ("a.csv") is never followed by a space+capital.
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z`\"'])", text) if s.strip()]
