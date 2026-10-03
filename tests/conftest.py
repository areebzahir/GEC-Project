"""Shared pytest fixtures and the repository root path."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "samples"


@pytest.fixture
def samples_dir() -> Path:
    return SAMPLES


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """An empty temporary directory to assemble a synthetic repository in."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo
