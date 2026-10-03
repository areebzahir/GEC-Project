"""Harvest abbreviation definitions from prose, e.g. "Bacterial abundance (BA)".

A compact re-implementation of the Schwartz-Hearst algorithm (Ref [23]): for each "(short form)"
in parentheses, test whether the words immediately before it can produce the short form's letters in
order. These pairs become README-local aliases that help the matcher link a documented long name to
a short column code (DESIGN.md sections 11, 14). Re-implemented from the paper; no code copied.
"""

from __future__ import annotations

import re

_PAREN_RE = re.compile(r"\b(?P<long>[\w][\w\s\-]+?)\s*\((?P<short>[A-Za-z][A-Za-z0-9\-]{0,9})\)")


def _is_valid_short_form(short: str) -> bool:
    """A plausible abbreviation: starts alphanumeric, 1-10 chars, has a letter."""
    return 1 <= len(short) <= 10 and any(c.isalpha() for c in short)


def _find_long_form(short: str, long_candidate: str) -> str | None:
    """Schwartz-Hearst matching: short-form chars must appear in order in the candidate.

    Walk both strings from the end; every character of the short form (ignoring case and non-alnum)
    must be found in the long form, and the first short-form char must align to the start of a word.
    """
    short_clean = [c.lower() for c in short if c.isalnum()]
    if not short_clean:
        return None
    long_chars = long_candidate.lower()

    s_index = len(short_clean) - 1
    l_index = len(long_chars) - 1
    while s_index >= 0:
        target = short_clean[s_index]
        if s_index == 0:
            # The first short-form letter must align to the start of a word in the long form.
            # Keep scanning left until we find it at a word boundary; this is what makes "BA"
            # resolve to "Bacterial abundance" rather than stopping at the 'a' inside a word.
            while l_index >= 0:
                if long_chars[l_index] == target and (l_index == 0 or not long_chars[l_index - 1].isalnum()):
                    return long_candidate[l_index:].strip()
                l_index -= 1
            return None
        # Later letters only need to appear in order anywhere to the left.
        while l_index >= 0 and long_chars[l_index] != target:
            l_index -= 1
        if l_index < 0:
            return None
        s_index -= 1
        l_index -= 1
    return None


def extract_abbreviations(text: str) -> dict[str, str]:
    """Return {short_form: long_form} pairs found in free text."""
    found: dict[str, str] = {}
    for match in _PAREN_RE.finditer(text):
        short = match.group("short").strip()
        long_candidate = match.group("long").strip()
        if not _is_valid_short_form(short):
            continue
        # Limit the long-form window to at most len(short)+5 words before the parenthesis.
        words = long_candidate.split()
        window = " ".join(words[-(len(short) + 5):])
        resolved = _find_long_form(short, window)
        if resolved and short not in found:
            found[short] = resolved
    return found
