"""research_normalizer: research repository -> standardized JSON.

The public entry point is :func:`run_pipeline` (added in ``pipeline.py``). Importing this package is
cheap; heavy dependencies (Polars, fastexcel) are imported lazily inside the stages that use them.
"""

from __future__ import annotations

SCHEMA_VERSION = "1.0.0"
TOOL_VERSION = "1.0.0"

__all__ = ["SCHEMA_VERSION", "TOOL_VERSION", "run_pipeline"]


def __getattr__(name: str):
    # Lazy re-export so `from research_normalizer import run_pipeline` works without importing the
    # whole pipeline (and Polars) at package-import time.
    if name == "run_pipeline":
        from .pipeline import run_pipeline

        return run_pipeline
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
