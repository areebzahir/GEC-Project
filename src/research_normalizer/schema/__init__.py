"""Canonical output schema and its JSON-Schema generator.

``document.py`` holds the Pydantic models that define the one standardized JSON format; this is the
single source of truth for the output (DESIGN.md section 15). ``provenance.py`` holds the small
Source/Provenance models shared across them.
"""

from __future__ import annotations

from .document import RepositoryDocument
from .provenance import Source

__all__ = ["RepositoryDocument", "Source"]
