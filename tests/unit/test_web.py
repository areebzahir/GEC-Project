"""Review gate + local dashboard API."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import jsonschema
import pytest

from research_normalizer.pipeline import run_pipeline
from research_normalizer.web import review
from research_normalizer.web.server import State, _safe_relpath, make_handler

from ..conftest import REPO_ROOT, SAMPLES

SCHEMA = json.loads((REPO_ROOT / "schemas" / "research_repository.schema.json").read_text(encoding="utf-8"))


def test_review_items_flag_phantom_and_low_confidence():
    pig = run_pipeline(SAMPLES / "pig_decomposition_hawaii")
    kinds = {(i["kind"], i["documented_name"]) for i in review.review_items(pig, 0.95)}
    assert kinds == {("phantom_variable", "FRESH")}

    cow = run_pipeline(SAMPLES / "dairy_cattle_energy")
    items = review.review_items(cow, 0.95)
    assert [(i["kind"], i["column"]) for i in items] == [("low_confidence", "BWsmth_chg")]
    assert review.review_items(cow, 0.90) == []


def test_reject_moves_name_to_unmatched_and_stays_schema_valid():
    cow = run_pipeline(SAMPLES / "dairy_cattle_energy")
    items = review.review_items(cow, 0.95)
    out = review.apply_decisions(cow, items, {items[0]["id"]: {"decision": "reject"}})
    ds = out.datasets[0]
    var = next(v for v in ds.variables if v.name == "BWsmth_chg")
    assert var.match.status.value == "unmatched" and var.description is None
    assert [u.name for u in ds.unmatched_documented_variables] == ["BWsmooth_chg"]
    assert out.summary.matched == cow.summary.matched - 1
    jsonschema.validate(out.model_dump(mode="json", by_alias=True), SCHEMA)
    assert cow.datasets[0].unmatched_documented_variables == []  # original untouched


def test_edits_fix_metadata_and_resolve_phantom():
    pig = run_pipeline(SAMPLES / "pig_decomposition_hawaii")
    ds = pig.datasets[0].id
    edits = {}
    edits[f"{ds}::DOC"] = review.clean_edit(pig, ds, "DOC", {"unit": "mgC/gSoil", "type": "number", "description": None}, {})
    assert set(edits[f"{ds}::DOC"]) == {"unit", "description"}  # type unchanged -> dropped
    edits[f"{ds}::FI"] = review.clean_edit(pig, ds, "FI", {"documented_name": "FRESH"}, {})
    with pytest.raises(ValueError, match="already linked"):
        review.check_link_unique(pig, edits, ds, "HIX", review.clean_edit(pig, ds, "HIX", {"documented_name": "FRESH"}, {}))

    out = review.preview(pig, edits)
    d0 = out.datasets[0]
    doc_var = next(v for v in d0.variables if v.name == "DOC")
    fi = next(v for v in d0.variables if v.name == "FI")
    assert doc_var.unit == "mgC/gSoil" and doc_var.description is None and doc_var.sources["unit"].method == "human_edit"
    assert fi.match.documented_name == "FRESH" and fi.match.method == "human_edited"
    assert d0.unmatched_documented_variables == [] and out.summary.warnings == 0
    jsonschema.validate(out.model_dump(mode="json", by_alias=True), SCHEMA)

    items = review.review_items(pig, 0.95)
    decisions = {}
    review.sync_auto_decisions(items, decisions, edits)
    assert decisions[items[0]["id"]]["note"] == review.AUTO_NOTE
    del edits[f"{ds}::FI"]
    review.sync_auto_decisions(items, decisions, edits)
    assert decisions == {}


def test_edit_rejects_bad_input():
    cow = run_pipeline(SAMPLES / "dairy_cattle_energy")
    ds = cow.datasets[0].id
    with pytest.raises(ValueError):
        review.clean_edit(cow, ds, "DMI", {"type": "banana"}, {})
    with pytest.raises(ValueError):
        review.clean_edit(cow, ds, "DMI", {"name": "x"}, {})
    with pytest.raises(ValueError):
        review.clean_edit(cow, ds, "nope", {"unit": "kg"}, {})


def test_acknowledge_all_skips_matches():
    items = [
        {"id": "a", "kind": "low_confidence"}, {"id": "b", "kind": "ambiguous"},
        {"id": "c", "kind": "undocumented_column"}, {"id": "d", "kind": "phantom_variable"},
        {"id": "e", "kind": "phantom_variable"},
    ]
    decisions = {"e": {"decision": "acknowledge", "note": ""}}
    assert review.acknowledge_all(items, decisions) == 2
    assert set(decisions) == {"c", "d", "e"}            # matches a/b still need a person
    assert decisions["e"]["note"] == ""                 # existing decisions are left alone
    assert review.acknowledge_all(items, decisions) == 0


def test_invalid_decision_rejected():
    with pytest.raises(ValueError):
        review.validate_decision({"kind": "phantom_variable"}, "accept")


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../b", "C:/x", ""])
def test_safe_relpath_rejects_escapes(bad):
    with pytest.raises(ValueError):
        _safe_relpath(bad)


@pytest.fixture
def server():
    state = State(SAMPLES)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _call(base, path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read())


def _wait(base, run_id):
    for _ in range(200):
        run = _call(base, f"/api/runs/{run_id}")
        if run["status"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_api_sample_review_export_flow(server):
    assert {s["name"] for s in _call(server, "/api/samples")} >= {"pig_decomposition_hawaii"}
    run = _call(server, "/api/runs", {"sample": "pig_decomposition_hawaii", "threshold": 0.95})
    run = _wait(server, run["id"])
    assert run["status"] == "review" and run["pending"] == 1
    assert run["events"][-1]["stage"] == "_end"

    with pytest.raises(urllib.error.HTTPError) as exc:  # export locked while pending
        _call(server, f"/api/runs/{run['id']}/export")
    assert exc.value.code == 409

    bulk = _call(server, f"/api/runs/{run['id']}/acknowledge-all", method="POST")
    assert bulk["acknowledged"] == 1 and bulk["status"] == "ready"   # the phantom FRESH notice
    doc = _call(server, f"/api/runs/{run['id']}/export")
    jsonschema.validate(doc, SCHEMA)
    assert _call(server, f"/api/runs/{run['id']}")["status"] == "exported"
    assert _call(server, f"/api/runs/{run['id']}/export?log=1")["decisions"][0]["decision"] == "acknowledge"


def test_api_edit_flows_into_export(server):
    run = _wait(server, _call(server, "/api/runs", {"sample": "dairy_cattle_energy", "threshold": 0.95})["id"])
    ds = run["document"]["datasets"][0]["id"]
    brief = _call(server, f"/api/runs/{run['id']}/edits",
                  {"dataset": ds, "column": "BWsmth_chg", "fields": {"unit": "kg/day", "label": "BW change"}})
    assert brief["pending"] == 0 and brief["edit_count"] == 1  # editing resolves the review item
    full = _call(server, f"/api/runs/{run['id']}")
    var = next(v for v in full["document"]["datasets"][0]["variables"] if v["name"] == "BWsmth_chg")
    assert var["unit"] == "kg/day"
    assert full["edits"][f"{ds}::BWsmth_chg"]["unit"]["value"] == "kg/day"
    doc = _call(server, f"/api/runs/{run['id']}/export")
    jsonschema.validate(doc, SCHEMA)
    assert next(v for v in doc["datasets"][0]["variables"] if v["name"] == "BWsmth_chg")["label"] == "BW change"
    assert _call(server, f"/api/runs/{run['id']}/export?log=1")["edits"][0]["column"] == "BWsmth_chg"

    with pytest.raises(urllib.error.HTTPError) as exc:
        _call(server, f"/api/runs/{run['id']}/edits", {"dataset": ds, "column": "DMI", "fields": {"type": "banana"}})
    assert exc.value.code == 400
    assert _call(server, f"/api/runs/{run['id']}/edits", {"dataset": ds, "column": "BWsmth_chg", "revert": True})["pending"] == 1


def test_api_source_excerpts(server):
    run = _wait(server, _call(server, "/api/runs", {"sample": "dairy_cattle_energy"})["id"])
    var = next(v for v in run["document"]["datasets"][0]["variables"] if v["name"] == "BWsmth_chg")
    src = var["sources"]["description"]
    lo, hi = src["lines"]
    text = _call(server, f"/api/runs/{run['id']}/source?file={src['file']}&start={lo}&end={hi}")
    assert text["kind"] == "text" and not text["changed"] and "BWsmooth_chg" in " ".join(l["text"] for l in text["lines"])

    table = _call(server, f"/api/runs/{run['id']}/source?file=CowEnergyBalanceData.tab")
    assert table["kind"] == "table" and table["header_row"] == 1 and "BWsmth_chg" in table["rows"][0]["cells"]

    for bad in ("../pyproject.toml", "nope.txt"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _call(server, f"/api/runs/{run['id']}/source?file={bad}")
        assert exc.value.code == 400


def test_api_delete_run_removes_it_and_its_upload(server):
    up = _call(server, "/api/uploads", method="POST")
    readme = (SAMPLES / "dairy_cattle_energy" / "100A_README.txt").read_bytes()
    req = urllib.request.Request(f"{server}/api/uploads/{up['id']}?path=README.txt", data=readme, method="PUT")
    urllib.request.urlopen(req, timeout=10).close()
    run = _wait(server, _call(server, "/api/runs", {"upload": up["id"]})["id"])

    assert _call(server, f"/api/runs/{run['id']}", method="DELETE") == {"deleted": run["id"]}
    assert all(r["id"] != run["id"] for r in _call(server, "/api/runs"))
    for path in (f"/api/runs/{run['id']}", ):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _call(server, path)
        assert exc.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as exc:  # upload folder is gone too
        _call(server, "/api/runs", {"upload": up["id"]})
    assert exc.value.code == 400


def test_api_upload_and_static(server):
    up = _call(server, "/api/uploads", method="POST")
    for f in (SAMPLES / "dairy_cattle_energy").iterdir():
        req = urllib.request.Request(f"{server}/api/uploads/{up['id']}?path=repo/{f.name}", data=f.read_bytes(), method="PUT")
        urllib.request.urlopen(req, timeout=10).close()
    run = _wait(server, _call(server, "/api/runs", {"upload": up["id"]})["id"])
    assert run["summary"]["matched"] == 24 and run["repository"] == "repo"

    with urllib.request.urlopen(server + "/", timeout=10) as res:
        assert b"research-normalizer" in res.read()
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(server + "/../pyproject.toml", timeout=10)
