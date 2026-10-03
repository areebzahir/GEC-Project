"""Find the missing-value conventions a README declares ("Missing data codes: NA", "-99 = missing").

Two entry points:
  * :func:`codes_from_key_value` for a key/value whose key names missing data
    ("Missing data codes", "Missing values", "NA values", "No data value").
  * :func:`codes_from_text` for prose ("Missing values are coded as -999", "-99 indicates missing",
    "blank cells indicate missing data", "values of 9999 represent no data").

Both return a list of literal codes (quotes stripped, order kept, ``""`` meaning blank cells), or
``None`` when the text says nothing about missing data. ``[]`` means "declared, but no codes"
("Missing data codes: none"). Where the codes apply (file, variable, repository) is decided by the
caller, which knows the surrounding context.
"""

from __future__ import annotations

import re

# Keys that introduce missing-value codes. Matched after lower-casing and trimming punctuation.
_MISSING_KEY_RE = re.compile(
    r"^(?:missing(?:\s+(?:data|value|values))?(?:\s+(?:codes?|values?|indicators?|flags?))?"
    r"|na\s+values?|no[\s-]?data\s+values?|nodata(?:\s+values?)?|null\s+values?|fill\s+values?)$"
)
# Values that mean "there are no missing codes" rather than being a code themselves.
_NO_CODES = {"none", "no", "nil", "not applicable", "no missing data", "no missing values",
             "there are no missing values", "there is no missing data"}
# Blank / empty cells used as the missing marker -> the empty-string code.
_BLANK_RE = re.compile(r"\b(?:blank|empty)\b", re.IGNORECASE)

# Prose patterns. Each captures the code text in the group "codes".
_TEXT_PATTERNS = [
    # "The missing data codes are NA and -99." / "missing value code = -9"
    re.compile(r"\bmissing\s+(?:data|value)\s+codes?\s*(?:are|is|:|=)\s*(?P<codes>.+?)(?:[.;](?:\s|$)|$)",
               re.IGNORECASE),
    # "Missing values are coded as -999." / "missing data is represented by NA"
    re.compile(r"\bmissing\s+(?:data|values?|observations|entries|measurements)?\s*(?:codes?\s+)?"
               r"(?:are|is|were|was|have\s+been|has\s+been)?\s*"
               r"(?:coded|represented|indicated|denoted|recorded|marked|given|shown|entered|reported|"
               r"written|filled|identified|flagged)\s+(?:as|by|with|using)\s+"
               r"(?P<codes>.+?)(?:[.;](?:\s|$)|$)", re.IGNORECASE),
    # "-99 indicates missing" / "NA denotes missing data" / "'.' means no data"
    re.compile(r"(?P<codes>[\"“‘'][^\"”’']*[\"”’']|\S+)\s+(?:indicates?|denotes?|represents?|means?|"
               r"signif(?:y|ies)|marks?|is\s+used\s+(?:for|to\s+(?:indicate|denote|mark)))\s+"
               r"(?:an?\s+)?(?:missing|no\s+data|not\s+available)", re.IGNORECASE),
    # "values of 9999 represent no data" / "a value of -1 indicates missing"
    re.compile(r"\bvalues?\s+of\s+(?P<codes>.+?)\s+(?:represents?|indicates?|denotes?|means?|signif(?:y|ies))"
               r"\s+(?:an?\s+)?(?:missing|no\s+data|not\s+available)", re.IGNORECASE),
]
# Blank-cell phrasing: "blank cells indicate missing data", "missing values are left blank".
_BLANK_TEXT_RE = re.compile(
    r"\b(?:blank|empty)\s+(?:cells?|fields?|values?|entries|strings?)?\s*"
    r"(?:indicate|represent|denote|mean|are|=)s?\s+(?:an?\s+)?(?:missing|no\s+data|not\s+available)"
    r"|\bmissing\s+(?:data|values?)\b[^.]{0,30}\b(?:left\s+)?(?:blank|empty)\b",
    re.IGNORECASE,
)

# Literal tokens that are codes even though they are lower-case words.
_KNOWN_CODES = {"na", "n/a", "n.a.", "nan", "null", "nd", "-", "--", "."}
# A label meaning "this code is a missing value" (value labels such as "-99 = missing").
# Only unambiguous wordings: "unknown" or "refused" can be real survey categories, so they stay labels.
_MISSING_LABEL_RE = re.compile(
    r"^(?:missing\b.*|(?:data\s+|value\s+)?not\s+(?:recorded|available|measured|collected)"
    r"|not\s+applicable|no\s+data|n/?a)$",
    re.IGNORECASE,
)


def is_missing_key(key: str) -> bool:
    """True when a key/value key introduces missing-value codes ("Missing data codes")."""
    return bool(_MISSING_KEY_RE.match(_clean_key(key)))


def is_missing_label(label: str) -> bool:
    """True when a value label means "missing" ("missing", "not recorded", "no data")."""
    return bool(_MISSING_LABEL_RE.match(label.strip().strip(".()[] ")))


def codes_from_key_value(key: str, value: str) -> list[str] | None:
    """Codes declared by a key/value line, or None when the key is not about missing data."""
    if not is_missing_key(key):
        return None
    # The value may itself be a sentence ("Missing values: blanks are used for missing data").
    from_text = codes_from_text(value)
    if from_text:
        return from_text
    return parse_code_list(value)


def codes_from_text(text: str) -> list[str] | None:
    """Codes stated in a prose sentence, or None when no missing-value statement is found."""
    codes: list[str] = []
    found = False
    if _BLANK_TEXT_RE.search(text):
        codes.append("")
        found = True
    for pattern in _TEXT_PATTERNS:
        for m in pattern.finditer(text):
            parsed = parse_code_list(m.group("codes"))
            if parsed:
                codes.extend(parsed)
                found = True
    return _unique(codes) if found else None


def parse_code_list(value: str) -> list[str]:
    """Split a declared code list into literal codes.

    ``"NA (not available)"`` -> ``["NA"]``; ``"."`` -> ``["."]``; ``"-99, -999 or NA"`` ->
    ``["-99", "-999", "NA"]``; ``"-9 = no data; -8 = not measured"`` -> ``["-9", "-8"]``;
    ``"none"`` -> ``[]``; ``"blank"`` -> ``[""]``.
    """
    text = value.strip()
    if text.lower().rstrip(".") in _NO_CODES:
        return []
    codes: list[str] = []
    # Explanations in brackets are not codes ("NA (not available)").
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
    for segment in re.split(r"[;,]|\s+or\s+|\s+and\s+", text):
        segment = segment.strip()
        if not segment:
            continue
        # "-9 = no data" / "NA: not available" / "ND - not detected": the code is the left side.
        left = re.split(r"\s*(?:=|:|\s-\s|\s–\s)\s*", segment, maxsplit=1)[0].strip()
        quoted = re.findall(r"[\"“‘'`](.*?)[\"”’'`]", left)
        if quoted:
            codes.extend(q.strip() for q in quoted)
            continue
        token = _first_code_token(left)
        if token is not None:
            codes.append(token)
        elif _BLANK_RE.search(left):
            codes.append("")
    return _unique(codes)


def _first_code_token(text: str) -> str | None:
    """The first word of ``text`` if it looks like a code (number, symbol, short upper-case token)."""
    words = text.split()
    if not words:
        return None
    token = words[0]
    if token.lower() in _KNOWN_CODES:
        return token
    # A trailing sentence period is punctuation, not part of the code ("coded as -999.").
    if re.search(r"\w", token):
        token = token.rstrip(".")
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", token):          # -999, 9999, 0.0
        return token
    if re.fullmatch(r"[^\w\s]{1,3}", token):                  # ".", "-", "*", "?"
        return token
    if re.fullmatch(r"[A-Z]{1,5}|[A-Za-z]{1,2}/[A-Za-z]{1,2}|NaN|NULL", token):  # NA, ND, N/A
        return token
    return None


def _clean_key(key: str) -> str:
    key = re.sub(r"^\s*\d+[.)]\s*", "", key)        # "3. Missing data codes"
    key = re.sub(r"[*_`#]", "", key)               # Markdown emphasis
    key = re.sub(r"\s*\([^)]*\)", "", key)         # "Missing values (if any)"
    return re.sub(r"\s+", " ", key).strip().strip(":").lower()


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
