"""Name normalisation and tokenisation, shared by matching and README field mapping.

Normalisation collapses the cosmetic differences we saw between README variable names and dataset
columns (``Date``/``date``, ``HP_GreenFeed``/``HP_Greenfeed``, ``milk-fat``/``milk_fat``) so that
exact and fuzzy comparisons line up (DESIGN.md section 14).
"""

from __future__ import annotations

import re
import unicodedata

# Characters treated as token separators inside a variable name.
_SEPARATORS = re.compile(r"[\s_\-.]+")
# A trailing unit/parenthetical suffix, e.g. "temp (C)" or "mass [kg]", removed before comparison.
_BRACKET_SUFFIX = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*$")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _strip_accents(text: str) -> str:
    # Decompose and drop combining marks so "région" compares equal to "region".
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_name(name: str) -> str:
    """Return a comparison key: accent-free, lowercase, separators and bracketed suffix removed.

    Example: ``"HP_GreenFeed"`` -> ``"hpgreenfeed"``; ``"Annual Income (CAD)"`` -> ``"annualincome"``.
    """
    text = _strip_accents(name).strip()
    text = _BRACKET_SUFFIX.sub("", text)
    text = _SEPARATORS.sub("", text)
    return text.lower()


def tokenize(name: str) -> list[str]:
    """Split a name into lowercase tokens, breaking on separators and camelCase boundaries.

    Example: ``"BWsmooth_chg"`` -> ``["bwsmooth", "chg"]`` becomes ``["bw", "smooth", "chg"]`` after
    the camel split on the leading ``BW``; ``"HP_GreenFeed"`` -> ``["hp", "green", "feed"]``.
    """
    text = _strip_accents(name).strip()
    text = _BRACKET_SUFFIX.sub("", text)
    parts = _SEPARATORS.split(text)
    tokens: list[str] = []
    for part in parts:
        if not part:
            continue
        # Break camelCase so "GreenFeed" -> "Green", "Feed".
        for piece in _CAMEL_BOUNDARY.split(part):
            if piece:
                tokens.append(piece.lower())
    return tokens
