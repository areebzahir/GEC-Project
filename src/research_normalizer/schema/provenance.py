"""Provenance models: where did a value come from?

Every value we extract from a README is traceable back to a file, a section and a line range, with
the recogniser that produced it (DESIGN.md section 16). Provenance is kept in parallel ``sources``
maps rather than wrapping every value, so the main output stays flat and readable.
"""

from __future__ import annotations

from pydantic import BaseModel


class Source(BaseModel):
    """A pointer back to the origin of a single extracted value."""

    file: str
    """Relative path of the file the value came from."""
    section: str | None = None
    """Heading of the README section, when applicable."""
    lines: tuple[int, int] | None = None
    """1-based inclusive line span within the file."""
    page: int | None = None
    """1-based page number, for values extracted from a PDF."""
    method: str | None = None
    """Name of the recogniser or stage that produced the value (e.g. 'delimited_line')."""
