"""Discovery, zip safety, and schema-drift guard (DESIGN.md sections 10, 15, 23)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from research_normalizer.config import PipelineConfig
from research_normalizer.discovery import FileRole, discover
from research_normalizer.issues import IssueCollector
from research_normalizer.schema.__main__ import check as schema_check

CFG = PipelineConfig()


def test_classifies_doc_and_data(tmp_repo: Path):
    (tmp_repo / "README.txt").write_text("Title: X\n")
    (tmp_repo / "data.csv").write_text("a,b\n1,2\n")
    issues = IssueCollector()
    with discover(tmp_repo, CFG, issues) as result:
        roles = {f.relative_path: f.role for f in result.files}
    assert roles["README.txt"] is FileRole.DOCUMENTATION
    assert roles["data.csv"] is FileRole.TABULAR


def test_zip_slip_member_rejected(tmp_path: Path):
    zp = tmp_path / "r.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("ok.csv", "a,b\n1,2\n")
        z.writestr("../escape.txt", "evil")
    issues = IssueCollector()
    with discover(zp, CFG, issues) as result:
        names = {f.relative_path for f in result.files}
    assert "ok.csv" in names
    assert any(i.code.value == "ZIP_UNSAFE_MEMBER" for i in issues.issues)


def test_missing_input_is_fatal(tmp_path: Path):
    issues = IssueCollector()
    result = discover(tmp_path / "nope", CFG, issues)
    assert result is None
    assert issues.has_fatal


def test_schema_committed_matches_models():
    # Fails if someone changed the models without regenerating schemas/*.json.
    assert schema_check(), "Run: python -m research_normalizer.schema"
