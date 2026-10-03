"""Variable matching: link documented variables to dataset columns.

``normalize`` provides the shared name-normalisation used across matching and README field mapping;
``scorers`` holds one function per matching stage; ``assign`` turns the score matrix into a
one-to-one assignment with ambiguity flags (DESIGN.md section 14).
"""

from __future__ import annotations

from .normalize import normalize_name, tokenize

__all__ = ["normalize_name", "tokenize"]
