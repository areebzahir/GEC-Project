"""Tabular reading: turn a data file into a uniform ``RawTable`` of string columns.

A ``TableReader`` adapter exists per format (delimited, Excel). Each returns the same ``RawTable``
so the rest of the pipeline is format-agnostic (DESIGN.md section 8, "Adapters"). Type inference and
profiling then run on that uniform structure.
"""

from __future__ import annotations

from .base import RawTable, read_table

__all__ = ["RawTable", "read_table"]
