"""Recognise variables the README says are intentionally NOT in the data.

Repositories sometimes document a variable and then state it was withheld, removed for privacy or
is available on request. Such a variable should not be reported as "documented but missing from the
data" (DESIGN.md section 11). We only look at a variable's own description/notes and at the title
of the section (or lead-in line) that lists it.

Missing *values* ("Missing data codes: NA") are a different thing and are never treated as absence;
none of the cues below mention "missing" for that reason.
"""

from __future__ import annotations

import re

# Phrases that, on their own, say the variable is not in the released files.
_ALWAYS_ABSENT = (
    r"withheld",
    r"redacted",
    r"not\s+released",
    r"not\s+shared",
    r"not\s+publicly\s+(?:available|released|shared)",
    r"(?:available|provided)\s+(?:up)?on\s+(?:reasonable\s+)?request",
)
# Phrases that say "not in the data" only when followed by the data/file they are absent from, or by
# the end of the clause ("removed from the public dataset", "not included."). This keeps
# "outliers were excluded from the analysis" or "not included in the mean" from counting.
_ABSENT_WITH_OBJECT = (
    r"not\s+included",
    r"not\s+provided",
    r"not\s+available\s+in\s+this",
    r"(?:was|were|has\s+been|have\s+been|is|are)?\s*(?:removed|excluded|omitted|dropped)",
)
# What a variable can be absent from.
_DATA_OBJECTS = (r"data|dataset|data\s*set|file|files|release|package|repository|deposit|"
                 r"spreadsheet|table|csv|version|archive|download|public")
_OBJECT_TAIL = rf"(?:\s+(?:in|from|with)\s+(?:the\s+|this\s+|these\s+)?(?:\w+\s+){{0,2}}(?:{_DATA_OBJECTS})\b|\s*(?:[.;:,)(]|$))"

_ABSENT_RE = re.compile(
    rf"\b(?:{'|'.join(_ALWAYS_ABSENT)})\b|\b(?:{'|'.join(_ABSENT_WITH_OBJECT)}){_OBJECT_TAIL}",
    re.IGNORECASE,
)
# Section titles / lead-in lines that announce a list of excluded variables.
_EXCLUSION_TITLE_RE = re.compile(
    r"\b(?:excluded|removed|withheld|omitted|redacted|dropped|not\s+included|not\s+released|"
    r"not\s+provided|not\s+shared|unreleased|restricted)\b",
    re.IGNORECASE,
)


def absence_reason(*texts: str | None) -> str | None:
    """Return the sentence that says a variable is not in the data, or None."""
    for text in texts:
        if not text:
            continue
        for sentence in _sentences(text):
            if _ABSENT_RE.search(sentence):
                return sentence.strip()
    return None


def is_exclusion_title(title: str | None) -> bool:
    """True when a heading or lead-in line announces excluded variables ("Variables not included").

    The title must also be about variables (or be a terse heading such as "Withheld"), so template
    lines like "Additional related data collected that was not included ..." do not count.
    """
    if not title or not _EXCLUSION_TITLE_RE.search(title):
        return False
    return bool(_VARIABLE_WORD_RE.search(title)) or len(title.split()) <= 3


_VARIABLE_WORD_RE = re.compile(r"\b(?:variables?|columns?|fields?|attributes?|measures?)\b", re.IGNORECASE)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text) if s.strip()]
