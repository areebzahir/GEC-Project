"""README / documentation parsing.

A documentation file is loaded to lines, segmented into blocks (headings, key/values, lists,
tables), then mined for project metadata (``project_fields``) and variable definitions
(``variable_layouts``), with abbreviations harvested from prose (``abbreviations``). Everything
carries line-range provenance (DESIGN.md section 11).
"""

from __future__ import annotations

__all__ = ["ParsedReadme", "parse_readme"]


def __getattr__(name: str):
    # Lazy export so importing submodules during development doesn't require parser.py to exist yet.
    if name in __all__:
        from . import parser

        return getattr(parser, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
