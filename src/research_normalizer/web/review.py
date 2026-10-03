"""Human-review gate layered on top of a finished :class:`RepositoryDocument`.

The pipeline itself never blocks: it records every match with a confidence. This module decides
which of those a person should look at (anything below a threshold, ambiguous, undocumented, or a
documented-but-absent "phantom" variable) and applies the reviewer's decisions to a copy of the
document, re-validating it so the exported JSON is still schema-valid.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..issues import Issue, IssueCode, Severity
from ..schema.document import ColumnRole, ColumnType, MatchStatus, RepositoryDocument, UnmatchedVariable
from ..schema.provenance import Source

DEFAULT_THRESHOLD = 0.95

# Variable fields a reviewer may correct. The column name itself is not editable: it is the key of
# every record, so changing it would mean rewriting the data, not the metadata.
EDITABLE_TEXT = ("label", "description", "unit", "notes")
EDITABLE = frozenset({*EDITABLE_TEXT, "documented_name", "type", "role", "missing_values"})
AUTO_NOTE = "resolved by editing the variable"

# Which decisions are allowed for each kind of review item.
ALLOWED: dict[str, frozenset[str]] = {
    "low_confidence": frozenset({"accept", "reject"}),
    "ambiguous": frozenset({"accept", "reject"}),
    "undocumented_column": frozenset({"acknowledge"}),
    "phantom_variable": frozenset({"acknowledge"}),
}


def review_items(doc: RepositoryDocument, threshold: float = DEFAULT_THRESHOLD) -> list[dict]:
    """Return every variable/match a human should decide on, in dataset order."""
    items: list[dict] = []
    for ds in doc.datasets:
        for v in ds.variables:
            m = v.match
            if m.status is MatchStatus.MATCHED and m.confidence >= threshold:
                continue
            kind = {
                MatchStatus.MATCHED: "low_confidence",
                MatchStatus.AMBIGUOUS: "ambiguous",
                MatchStatus.UNMATCHED: "undocumented_column",
            }[m.status]
            src = v.sources.get("description")
            items.append({
                "id": f"{ds.id}::col::{v.name}",
                "kind": kind,
                "dataset": ds.id,
                "file": ds.file,
                "column": v.name,
                "documented_name": m.documented_name,
                "confidence": m.confidence,
                "method": m.method,
                "evidence": list(m.evidence),
                "alternatives": [a.model_dump() for a in m.alternatives],
                "description": v.description,
                "unit": v.unit,
                "source": src.model_dump() if src else None,
            })
        for u in ds.unmatched_documented_variables:
            items.append({
                "id": f"{ds.id}::doc::{u.name}",
                "kind": "phantom_variable",
                "dataset": ds.id,
                "file": ds.file,
                "column": None,
                "documented_name": u.name,
                "confidence": 0.0,
                "method": None,
                "evidence": ["documented in README but no column in the data file"],
                "alternatives": [],
                "description": u.description,
                "unit": None,
                "source": {"lines": list(u.lines)} if u.lines else None,
            })
    return items


def validate_decision(item: dict, decision: str) -> None:
    if decision not in ALLOWED[item["kind"]]:
        allowed = ", ".join(sorted(ALLOWED[item["kind"]]))
        raise ValueError(f"'{decision}' is not valid for a {item['kind']} item (allowed: {allowed}).")


# --------------------------------------------------------------------------- variable edits
def _find(doc: RepositoryDocument, dataset: str, column: str):
    ds = next((d for d in doc.datasets if d.id == dataset), None)
    if ds is None:
        raise ValueError(f"Unknown dataset: {dataset!r}")
    var = next((v for v in ds.variables if v.name == column), None)
    if var is None:
        raise ValueError(f"Unknown column {column!r} in {ds.file}")
    return ds, var


def _current(var, field: str):
    if field == "documented_name":
        return var.match.documented_name
    if field in ("type", "role"):
        return getattr(var, field).value
    if field == "missing_values":
        return sorted(var.missing_values)
    return getattr(var, field)


def clean_edit(doc: RepositoryDocument, dataset: str, column: str, fields: dict, existing: dict) -> dict:
    """Validate requested changes against the pipeline's original document.

    Returns the variable's full edit set as ``{field: {"value": new, "original": old}}``. A field set
    back to its original value is dropped, so "editing it back" is the same as reverting it.
    """
    _, var = _find(doc, dataset, column)
    unknown = set(fields) - EDITABLE
    if unknown:
        raise ValueError(f"Not editable: {', '.join(sorted(unknown))}. Editable: {', '.join(sorted(EDITABLE))}.")
    out = dict(existing)
    for field, raw in fields.items():
        if field in EDITABLE_TEXT or field == "documented_name":
            value = (str(raw).strip()[:4000] if raw is not None else "") or None
        elif field == "type":
            value = ColumnType(raw).value  # ValueError on an unknown type
        elif field == "role":
            value = ColumnRole(raw).value
        else:  # missing_values: list or comma-separated string
            parts = raw if isinstance(raw, list) else str(raw or "").split(",")
            value = sorted({str(p).strip() for p in parts if str(p).strip()})
        original = _current(var, field)
        if value == original:
            out.pop(field, None)
        else:
            out[field] = {"value": value, "original": original}
    return out


def check_link_unique(doc: RepositoryDocument, edits: dict, dataset: str, column: str, cleaned: dict) -> None:
    """Refuse to link two columns of one dataset to the same README variable."""
    target = cleaned.get("documented_name", {}).get("value")
    if not target:
        return
    ds, _ = _find(doc, dataset, column)
    for v in ds.variables:
        if v.name == column:
            continue
        change = edits.get(f"{dataset}::{v.name}", {}).get("documented_name")
        linked = change["value"] if change else (v.match.documented_name if v.match.status is MatchStatus.MATCHED else None)
        if linked == target:
            raise ValueError(f"'{target}' is already linked to column '{v.name}'. Unlink it there first.")


def sync_auto_decisions(items: list[dict], decisions: dict[str, dict], edits: dict) -> None:
    """Editing a flagged variable resolves its review item; reverting the edit re-opens it."""
    edited_cols = {tuple(k.split("::", 1)) for k in edits}
    linked = {(k.split("::", 1)[0], f["documented_name"]["value"])
              for k, f in edits.items() if f.get("documented_name", {}).get("value")}
    for it in items:
        related = ((it["dataset"], it["column"]) in edited_cols if it["column"]
                   else (it["dataset"], it["documented_name"]) in linked)
        dec = decisions.get(it["id"])
        if related and dec is None:
            decisions[it["id"]] = {
                "decision": "accept" if "accept" in ALLOWED[it["kind"]] else "acknowledge",
                "note": AUTO_NOTE, "decided_at": datetime.now(timezone.utc).isoformat(),
            }
        elif not related and dec and dec.get("note") == AUTO_NOTE:
            del decisions[it["id"]]


def _apply_edits(doc: RepositoryDocument, edits: dict) -> None:
    """Apply reviewer edits in place (on a copy). Edited values get a 'reviewer' provenance entry."""
    reviewer = Source(file="reviewer", method="human_edit")
    for key, fields in edits.items():
        ds, var = _find(doc, *key.split("::", 1))
        for field, change in fields.items():
            value = change["value"]
            if field == "documented_name":
                m = var.match
                if value:
                    phantom = next((u for u in ds.unmatched_documented_variables if u.name == value), None)
                    if phantom is not None:
                        ds.unmatched_documented_variables.remove(phantom)
                        if "description" not in fields and not var.description:
                            var.description = phantom.description
                        doc.issues = [i for i in doc.issues if not (
                            i.code is IssueCode.DOCUMENTED_VARIABLE_NOT_IN_DATA
                            and f"'{value}'" in i.message and ds.file in i.message)]
                    m.status, m.documented_name, m.confidence = MatchStatus.MATCHED, value, 1.0
                    m.evidence.append(f"reviewer linked this column to '{value}'")
                else:
                    m.status, m.documented_name, m.confidence = MatchStatus.UNMATCHED, None, 0.0
                    m.evidence.append("reviewer removed the README link")
                m.method, m.warning = "human_edited", None
                continue
            if field == "type":
                var.type = ColumnType(value)
            elif field == "role":
                var.role = ColumnRole(value)
            elif field == "missing_values":
                var.missing_values = list(value)
            else:
                setattr(var, field, value)
            var.sources[field] = reviewer


def _finish(out: RepositoryDocument) -> RepositoryDocument:
    variables = [v for d in out.datasets for v in d.variables]
    out.summary.matched = sum(1 for v in variables if v.match.status is MatchStatus.MATCHED)
    out.summary.undocumented = sum(1 for v in variables if v.match.status is MatchStatus.UNMATCHED)
    out.summary.warnings = sum(1 for i in out.issues if i.severity is Severity.WARNING)
    out.summary.errors = sum(1 for i in out.issues if i.severity in (Severity.ERROR, Severity.FATAL))
    # Round-trip through validation so the result is guaranteed schema-valid.
    return RepositoryDocument.model_validate(out.model_dump(by_alias=True))


def preview(doc: RepositoryDocument, edits: dict) -> RepositoryDocument:
    """The document as the reviewer currently sees it: pipeline output plus edits (not decisions)."""
    if not edits:
        return doc
    out = doc.model_copy(deep=True)
    _apply_edits(out, edits)
    return _finish(out)


# --------------------------------------------------------------------------- decisions
def apply_decisions(doc: RepositoryDocument, items: list[dict], decisions: dict[str, dict],
                    edits: dict | None = None) -> RepositoryDocument:
    """Return a new, re-validated document with reviewer decisions, then edits, applied.

    accept      -> match kept, marked matched, evidence notes the approval
    reject      -> column becomes undocumented; the README name moves to unmatched_documented_variables
    acknowledge -> no data change (the issue is already recorded); logged in evidence where possible
    edits       -> applied last, so a hand correction always wins over an accept/reject
    """
    out = doc.model_copy(deep=True)
    by_id = {i["id"]: i for i in items}
    datasets = {d.id: d for d in out.datasets}
    extra_issues: list[Issue] = []

    for item_id, dec in decisions.items():
        item = by_id.get(item_id)
        if item is None or item["column"] is None:
            continue
        ds = datasets[item["dataset"]]
        var = next(v for v in ds.variables if v.name == item["column"])
        if dec["decision"] == "accept":
            var.match.status = MatchStatus.MATCHED
            var.match.evidence.append("approved by human reviewer")
        elif dec["decision"] == "reject":
            documented = var.match.documented_name
            src = var.sources.get("description")
            if documented:
                ds.unmatched_documented_variables.append(UnmatchedVariable(
                    name=documented, description=var.description, lines=src.lines if src else None,
                ))
            var.match.status = MatchStatus.UNMATCHED
            var.match.documented_name = None
            var.match.confidence = 0.0
            var.match.method = "human_rejected"
            var.match.evidence.append(f"reviewer rejected link to '{documented}'")
            var.label = var.description = var.unit = var.notes = None
            var.value_labels = []
            var.sources = {}
            extra_issues.append(Issue.make(
                IssueCode.MATCH_LOW_CONFIDENCE, Severity.INFO,
                f"Reviewer rejected the match {item['column']} -> {documented}.",
                file=ds.file, column=item["column"],
            ))
        elif dec["decision"] == "acknowledge":
            var.match.evidence.append("acknowledged by human reviewer")

    out.issues.extend(extra_issues)
    _apply_edits(out, edits or {})
    return _finish(out)


def decision_log(run_id: str, items: list[dict], decisions: dict[str, dict], edits: dict | None = None) -> dict:
    by_id = {i["id"]: i for i in items}
    return {
        "run_id": run_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "decisions": [{"item": by_id[k], **v} for k, v in decisions.items() if k in by_id],
        "edits": [{"dataset": k.split("::", 1)[0], "column": k.split("::", 1)[1], "changes": f}
                  for k, f in (edits or {}).items()],
    }
