"""Turn documentation lines into structured blocks grouped under sections.

The segmenter recognises headings, key/value lines, list items, table rows and horizontal rules
(DESIGN.md section 11). It is deliberately layout-tolerant: the two sample READMEs use completely
different conventions (numbered ``1. Title:`` fields with ``-`` rule lines vs. ALL-CAPS headings),
and unseen repositories will differ again. Every block keeps its 1-based line span for provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class BlockType(StrEnum):
    HEADING = "heading"
    KEY_VALUE = "key_value"
    LIST_ITEM = "list_item"
    TABLE_ROW = "table_row"
    RULE = "rule"
    TEXT = "text"


@dataclass(slots=True)
class Block:
    """One recognised line (or merged key/value spanning lines)."""

    type: BlockType
    text: str
    line_start: int             # 1-based
    line_end: int
    key: str | None = None      # for KEY_VALUE
    value: str | None = None
    cells: list[str] = field(default_factory=list)  # for TABLE_ROW


@dataclass(slots=True)
class Section:
    """A heading and the blocks beneath it (until the next heading)."""

    title: str | None
    line_start: int
    line_end: int
    blocks: list[Block] = field(default_factory=list)
    level: int | None = None    # Markdown heading depth (# = 1); None for plain-text headings


# A key/value line: optional leading list number, then "Key: value". Key is <=60 chars and must not
# itself look like a sentence. We guard against splitting URLs and times by requiring the key to
# contain no "//" and the colon not to be between two digits (that would be a time).
_KEY_VALUE_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?(?P<key>[^:]{1,60}?):\s*(?P<value>.*)$")
_LIST_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+(?P<item>.+)$")
_RULE_RE = re.compile(r"^\s*(?:[-=_*]{3,}|-)\s*$")  # a lone '-' is used as a rule in the dairy README
_PIPE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_PROMPT_RE = re.compile(r"<[^>]{3,}>")  # template prompts like <provide at least two contacts>
# Column gap in a whitespace-aligned table (PDF text, fixed-width codebooks): a tab or 2+ spaces.
_COLUMN_GAP_RE = re.compile(r"\t+|\s{2,}")


def looks_like_aligned_row(line: str) -> bool:
    """True when a line splits into 2+ columns on tabs / runs of spaces (an aligned table row).

    A leading list number ("1.  Introduction") does not count as a column.
    """
    cells = [c for c in _COLUMN_GAP_RE.split(line.strip()) if c]
    if cells and re.fullmatch(r"\d+[.)]?", cells[0]):
        cells = cells[1:]
    return len(cells) >= 2


def _strip_quote_noise(line: str) -> str:
    """Remove spreadsheet-export quoting debris seen in the dairy README.

    Examples: ``" Description: unique cow identifier"`` -> ``Description: unique cow identifier``;
    a trailing smart quote ``”`` left dangling is dropped. We only strip a quote that wraps the whole
    line, so quoted values inside a sentence are preserved.
    """
    s = line.strip()
    if len(s) >= 2 and s[0] in "\"'“”" and s[-1] in "\"'“”":
        s = s[1:-1]
    # A line that merely starts with a stray wrapping quote (unbalanced from CSV export).
    elif s[:1] in "\"'“”" and s.count('"') <= 1:
        s = s[1:]
    # Drop a single dangling smart/plain quote at the very end.
    if s[-1:] in "\"”" and s.count('"') % 2 == 1:
        s = s[:-1]
    return s.strip()


def _looks_like_heading(line: str, nxt: str | None, prev_is_rule: bool) -> bool:
    """Plain-text heading heuristic for READMEs without Markdown ``#`` headings.

    Three shapes count as a heading: an ALL-CAPS short line, a short line right after a rule line,
    or a short line ending in ``:`` with nothing after it. A line with a value after its colon is
    always a key/value instead. The word limits keep ordinary sentences from becoming headings.
    """
    s = line.strip()
    if not s:
        return False
    # An aligned table header such as "VARIABLE    DESCRIPTION    UNITS" is a table row, not a title.
    if looks_like_aligned_row(s):
        return False
    kv = _KEY_VALUE_RE.match(s)
    if kv:
        # A line with a value after the colon is a key/value, never a heading.
        if kv.group("value").strip():
            return False
        # A single-word key with an empty value (e.g. "ORCID:", "Email:") is an *empty* field in a
        # person/record block, not a section heading. Keeping it as a key/value stops person groups
        # from being split apart.
        if len(kv.group("key").split()) <= 1:
            return False
    words = s.split()
    # ALL-CAPS institutional headings, e.g. "METHODOLOGICAL INFORMATION".
    letters = [c for c in s if c.isalpha()]
    if letters and s.upper() == s and len(words) <= 8 and not s.endswith("."):
        return True
    # A short line sitting between rule lines (dairy README: '-' / 'General information' / '-').
    if prev_is_rule and len(words) <= 8 and not s.endswith("."):
        return True
    # A short line that ends with ':' and has no value after it ("Variable List:").
    if s.endswith(":") and len(words) <= 6:
        return True
    return False


def _markdown_heading(line: str) -> str | None:
    m = re.match(r"^\s*(#{1,6})\s+(?P<t>.+?)\s*#*\s*$", line)
    return m.group("t").strip() if m else None


def _markdown_level(line: str) -> int:
    return len(line.strip()) - len(line.strip().lstrip("#"))


def segment(lines: list[str]) -> list[Section]:
    """Group lines into sections of typed blocks (DESIGN.md section 11)."""
    sections: list[Section] = [Section(title=None, line_start=1, line_end=len(lines))]
    prev_is_rule = False

    i = 0
    while i < len(lines):
        # Strip inline template-prompt placeholders (e.g. "Variable List: <list name, unit ...>").
        # These are instructions from the README template, never real content, and leaving them in
        # would turn a section heading like "Variable List:" into a key/value and hide the section.
        raw = _PROMPT_RE.sub("", lines[i]).rstrip()
        lineno = i + 1
        stripped = raw.strip()

        if not stripped:
            prev_is_rule = False
            i += 1
            continue

        # Horizontal rule.
        if _RULE_RE.match(stripped):
            sections[-1].blocks.append(Block(BlockType.RULE, stripped, lineno, lineno))
            prev_is_rule = True
            i += 1
            continue

        # Markdown or Setext heading.
        md = _markdown_heading(raw)
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else None
        is_setext = bool(nxt and re.match(r"^\s*(={3,}|-{3,})\s*$", nxt)) and len(stripped) <= 80
        if md is not None or is_setext:
            title = md if md is not None else stripped
            level = _markdown_level(raw) if md is not None else (1 if nxt and nxt.startswith("=") else 2)
            sections.append(Section(title=title, line_start=lineno, line_end=lineno, level=level))
            prev_is_rule = False
            i += 2 if is_setext else 1
            continue

        # Pipe table row (GFM, Ref [22]).
        if _PIPE_ROW_RE.match(raw):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not all(set(c) <= set("-: ") for c in cells):  # skip the |---|---| separator row
                sections[-1].blocks.append(Block(BlockType.TABLE_ROW, stripped, lineno, lineno, cells=cells))
            prev_is_rule = False
            i += 1
            continue

        # Prose heading (ALL-CAPS, between-rules, or trailing-colon).
        if _looks_like_heading(raw, nxt, prev_is_rule):
            sections.append(Section(title=_strip_quote_noise(stripped).rstrip(":"), line_start=lineno, line_end=lineno))
            prev_is_rule = False
            i += 1
            continue

        # Key/value, with a continuation gather for wrapped values.
        cleaned = _strip_quote_noise(raw)
        kv = _KEY_VALUE_RE.match(cleaned)
        if kv and _is_real_key(kv.group("key"), kv.group("value")):
            key = kv.group("key").strip()
            value = kv.group("value").strip()
            end = lineno
            # Gather continuation lines (indented or non-key text) into the value.
            j = i + 1
            while j < len(lines):
                follow = lines[j].strip()
                if not follow or _KEY_VALUE_RE.match(_strip_quote_noise(lines[j])) and _is_real_key(
                    *_split_kv(_strip_quote_noise(lines[j]))
                ):
                    break
                if _looks_like_heading(lines[j], None, False) or _RULE_RE.match(follow):
                    break
                # The next row of an aligned table is its own line, not a wrapped value.
                if looks_like_aligned_row(follow):
                    break
                value = f"{value} {_strip_quote_noise(lines[j])}".strip()
                end = j + 1
                j += 1
            sections[-1].blocks.append(
                Block(BlockType.KEY_VALUE, cleaned, lineno, end, key=key, value=value)
            )
            prev_is_rule = False
            i = j
            continue

        # List item.
        lm = _LIST_RE.match(raw)
        if lm:
            sections[-1].blocks.append(Block(BlockType.LIST_ITEM, lm.group("item").strip(), lineno, lineno))
            prev_is_rule = False
            i += 1
            continue

        # Plain text.
        sections[-1].blocks.append(Block(BlockType.TEXT, cleaned, lineno, lineno))
        prev_is_rule = False
        i += 1

    # Close section line spans.
    for s_index, sec in enumerate(sections):
        if sec.blocks:
            sec.line_end = sec.blocks[-1].line_end
    return [s for s in sections if s.blocks or s.title]


def _split_kv(line: str) -> tuple[str, str]:
    m = _KEY_VALUE_RE.match(line)
    return (m.group("key"), m.group("value")) if m else ("", "")


def _is_real_key(key: str, value: str) -> bool:
    """Reject false key/value splits: URLs, times, and sentence-like 'keys'.

    A real metadata key is short and label-like. We reject when the 'key' contains a scheme (https)
    or is long and sentence-like, which prevents splitting ``http://doi.org/...`` at its colon.
    """
    key = key.strip()
    if not key:
        return False
    # Reject when the colon we split on is actually part of a URL scheme: either the key already
    # contains/ends with a scheme word, or the value begins with "//" (the rest of "https://...").
    if "//" in key or key.lower().split()[-1] in {"http", "https", "ftp", "doi", "ftps"}:
        return False
    if value.strip().startswith("//"):
        return False
    # A column gap inside the "key" means this is an aligned table row that happens to hold a colon.
    if _COLUMN_GAP_RE.search(key):
        return False
    # A key with many words that reads like a sentence is probably prose, unless it is a known long
    # template label (handled by project_fields via fuzzy matching, which tolerates these).
    if len(key.split()) > 10:
        return False
    return True
