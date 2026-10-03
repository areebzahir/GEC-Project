"""Generate the committed JSON Schema from the Pydantic models.

Run with ``python -m research_normalizer.schema`` to (re)write
``schemas/research_repository.schema.json``. A test compares the committed file against a freshly
generated one, so the schema and the models can never silently drift (DESIGN.md section 15).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .document import RepositoryDocument

# Repo root is four levels up: schema/ -> research_normalizer/ -> src/ -> repo root.
_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "research_repository.schema.json"


def build_schema() -> dict:
    """Return the JSON Schema (Draft 2020-12) for the canonical document."""
    return RepositoryDocument.model_json_schema()


def render() -> str:
    """Deterministic, pretty-printed JSON text for the schema file."""
    return json.dumps(build_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write() -> Path:
    _SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SCHEMA_PATH.write_text(render(), encoding="utf-8")
    return _SCHEMA_PATH


def check() -> bool:
    """True if the committed schema matches the models. Used by ``--check`` and tests."""
    if not _SCHEMA_PATH.exists():
        return False
    return _SCHEMA_PATH.read_text(encoding="utf-8") == render()


if __name__ == "__main__":
    if "--check" in sys.argv:
        ok = check()
        print("schema up to date" if ok else "schema OUT OF DATE: run python -m research_normalizer.schema")
        sys.exit(0 if ok else 1)
    path = write()
    print(f"wrote {path}")
