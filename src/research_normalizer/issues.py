"""Issue model and catalogue.

Every problem the pipeline meets becomes an :class:`Issue` instead of a crash or a silent skip
(DESIGN.md section 18, rubric "Error Handling"). An issue always carries:

* a severity, so the caller knows whether output is still usable;
* a plain-language ``message`` for a non-technical user;
* a ``technical_detail`` for a developer;
* a ``location`` (file / lines / sheet) so the problem can be found;
* an optional ``suggestion`` for how to fix it.

Codes live in one enum so the set of things that can go wrong is discoverable in a single place.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    """How badly a problem affects the result."""

    FATAL = "fatal"          # no output possible; run aborts
    ERROR = "error"          # one file/sheet lost; run continues
    WARNING = "warning"      # output produced, a human should check
    INFO = "info"            # a notable but harmless decision


class IssueCode(StrEnum):
    """Catalogue of everything that can go wrong, grouped by stage."""

    # discovery / input
    INPUT_NOT_FOUND = "INPUT_NOT_FOUND"
    NO_FILES_FOUND = "NO_FILES_FOUND"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_FILES = "TOO_MANY_FILES"
    UNSUPPORTED_FILE = "UNSUPPORTED_FILE"
    ZIP_UNSAFE_MEMBER = "ZIP_UNSAFE_MEMBER"
    ZIP_LIMIT_EXCEEDED = "ZIP_LIMIT_EXCEEDED"

    # decoding
    ENCODING_FALLBACK = "ENCODING_FALLBACK"
    ENCODING_REPLACED_CHARS = "ENCODING_REPLACED_CHARS"

    # readme
    README_NOT_FOUND = "README_NOT_FOUND"
    README_MULTIPLE = "README_MULTIPLE"
    README_UNREADABLE = "README_UNREADABLE"
    CONFLICTING_METADATA = "CONFLICTING_METADATA"

    # delimited parsing
    CSV_RAGGED_ROWS = "CSV_RAGGED_ROWS"
    CSV_QUOTE_FALLBACK = "CSV_QUOTE_FALLBACK"
    CSV_PARSE_FAILED = "CSV_PARSE_FAILED"
    DELIMITER_UNCERTAIN = "DELIMITER_UNCERTAIN"

    # excel
    EXCEL_CORRUPT = "EXCEL_CORRUPT"
    EXCEL_HIDDEN_SHEET = "EXCEL_HIDDEN_SHEET"
    EXCEL_EMPTY_SHEET = "EXCEL_EMPTY_SHEET"
    EXCEL_FORMULA_NO_CACHED_VALUE = "EXCEL_FORMULA_NO_CACHED_VALUE"

    # header
    HEADER_NOT_FOUND = "HEADER_NOT_FOUND"
    HEADER_AMBIGUOUS = "HEADER_AMBIGUOUS"
    HEADER_DUPLICATE = "HEADER_DUPLICATE"
    HEADER_BLANK = "HEADER_BLANK"

    # type inference
    AMBIGUOUS_DATE_FORMAT = "AMBIGUOUS_DATE_FORMAT"
    POSSIBLE_UNDECLARED_MISSING_CODE = "POSSIBLE_UNDECLARED_MISSING_CODE"
    DECLARED_COUNT_MISMATCH = "DECLARED_COUNT_MISMATCH"

    # matching
    MATCH_AMBIGUOUS = "MATCH_AMBIGUOUS"
    MATCH_LOW_CONFIDENCE = "MATCH_LOW_CONFIDENCE"
    VARIABLE_UNDOCUMENTED = "VARIABLE_UNDOCUMENTED"
    DOCUMENTED_VARIABLE_NOT_IN_DATA = "DOCUMENTED_VARIABLE_NOT_IN_DATA"
    DOCUMENTED_VARIABLE_INTENTIONALLY_ABSENT = "DOCUMENTED_VARIABLE_INTENTIONALLY_ABSENT"
    DOCUMENTED_VARIABLE_IN_OTHER_FILE = "DOCUMENTED_VARIABLE_IN_OTHER_FILE"
    DOCUMENTATION_UNASSIGNED = "DOCUMENTATION_UNASSIGNED"

    # validation / internal
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class IssueLocation(BaseModel):
    """Where a problem lives, so a user (or a future GUI) can jump straight to it."""

    file: str | None = None
    sheet: str | None = None
    lines: tuple[int, int] | None = None  # 1-based inclusive line span
    column: str | None = None


class Issue(BaseModel):
    """One categorized, explainable problem."""

    code: IssueCode
    severity: Severity
    message: str                       # plain language
    technical_detail: str | None = None
    location: IssueLocation | None = None
    suggestion: str | None = None

    @classmethod
    def make(
        cls,
        code: IssueCode,
        severity: Severity,
        message: str,
        *,
        technical_detail: str | None = None,
        file: str | None = None,
        sheet: str | None = None,
        lines: tuple[int, int] | None = None,
        column: str | None = None,
        suggestion: str | None = None,
    ) -> "Issue":
        """Convenience constructor that builds the location from loose parts."""
        location = None
        if any(v is not None for v in (file, sheet, lines, column)):
            location = IssueLocation(file=file, sheet=sheet, lines=lines, column=column)
        return cls(
            code=code,
            severity=severity,
            message=message,
            technical_detail=technical_detail,
            location=location,
            suggestion=suggestion,
        )


class IssueCollector:
    """Accumulates issues during a run and answers simple questions about them.

    Passing one of these into each stage keeps stages pure of printing/logging: they just record.
    """

    def __init__(self) -> None:
        self._issues: list[Issue] = []

    def add(self, issue: Issue) -> Issue:
        self._issues.append(issue)
        return issue

    def extend(self, issues: list[Issue]) -> None:
        self._issues.extend(issues)

    @property
    def issues(self) -> list[Issue]:
        return list(self._issues)

    def count(self, severity: Severity) -> int:
        return sum(1 for i in self._issues if i.severity is severity)

    @property
    def has_fatal(self) -> bool:
        return self.count(Severity.FATAL) > 0

    @property
    def has_error(self) -> bool:
        return self.count(Severity.ERROR) > 0
