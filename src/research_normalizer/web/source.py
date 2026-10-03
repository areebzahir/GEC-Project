"""Read-only excerpts of a run's original files, so a reviewer can check what was extracted.

README excerpts go through the same loader the parser used, so line numbers match the provenance
recorded in the document. Data excerpts are parsed with the dialect the pipeline detected, so the
reviewer sees exactly where the header was found. Only files listed in the run's ``files`` array
can be read, and zips are re-extracted with the pipeline's own zip-slip/zip-bomb guards.
"""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path

from ..config import DELIMITED_EXTENSIONS, PipelineConfig
from ..discovery import _extract_zip
from ..issues import IssueCollector
from ..readme.loaders import load_document
from ..schema.document import RepositoryDocument
from ..text_decoding import decode_bytes

MAX_LINES = 400
MAX_ROWS = 60


def repository_root(input_path: Path, scratch: Path) -> Path:
    """Directory the run's relative paths are relative to (extracting a zip into ``scratch`` once)."""
    input_path = input_path.resolve()
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        if not scratch.exists():
            scratch.mkdir(parents=True)
            _extract_zip(input_path, scratch, PipelineConfig(), IssueCollector())
        return scratch
    return input_path if input_path.is_dir() else input_path.parent


def _locate(doc: RepositoryDocument, root: Path, rel: str):
    record = next((f for f in doc.files if f.path == rel), None)
    if record is None:
        raise ValueError("That file is not part of this run.")
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise FileNotFoundError(f"{rel} is no longer available on disk.")
    return record, path


def excerpt(doc: RepositoryDocument, root: Path, rel: str, start: int = 1, end: int | None = None) -> dict:
    """Return lines (documentation) or parsed rows (delimited data) for one repository file."""
    record, path = _locate(doc, root, rel)
    data = path.read_bytes()
    changed = hashlib.sha256(data).hexdigest() != record.sha256
    base = {"file": rel, "role": record.role, "changed": changed}

    if record.role == "documentation":
        loaded = load_document(path, path.suffix.lower(), PipelineConfig())
        total = len(loaded.lines)
        start = max(1, start)
        end = min(total, end or total, start + MAX_LINES - 1)
        return {**base, "kind": "text", "encoding": loaded.encoding, "total": total,
                "start": start, "end": end,
                "lines": [{"n": n, "text": loaded.lines[n - 1]} for n in range(start, end + 1)]}

    ds = next((d for d in doc.datasets if d.file == rel), None)
    if record.role == "tabular" and ds and path.suffix.lower() in DELIMITED_EXTENSIONS and ds.structure.delimiter:
        decoded = decode_bytes(data[: PipelineConfig().sample_bytes * 4])
        reader = csv.reader(io.StringIO(decoded.text), delimiter=ds.structure.delimiter,
                            quotechar=ds.structure.quote_char or '"')
        rows = []
        for i, row in enumerate(reader, start=1):
            if i > MAX_ROWS + (ds.structure.header_row or 0):
                break
            rows.append({"n": i, "cells": row})
        return {**base, "kind": "table", "encoding": decoded.encoding, "header_row": ds.structure.header_row,
                "delimiter": ds.structure.delimiter, "rows": rows, "total_rows": ds.row_count}

    return {**base, "kind": "unavailable",
            "reason": "No raw preview for this format (spreadsheets and binary files). The parsed records are shown instead."}
