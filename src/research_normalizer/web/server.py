"""Local dashboard server (stdlib only, no extra dependencies).

    research-normalizer-web [--host 127.0.0.1] [--port 8765]

Serves the static frontend and a small JSON API over :func:`run_pipeline`:

    GET  /api/samples                      sample repositories under ./samples
    GET  /api/runs                         run list (no documents)
    POST /api/runs                         {"sample"|"path"|"upload": ..., "threshold": 0.95}
    GET  /api/runs/<id>                    full run: document, events, review items, decisions
    GET  /api/runs/<id>/events             Server-Sent Events stream of pipeline events
    POST /api/runs/<id>/decisions          {"item_id": ..., "decision": accept|reject|acknowledge|undo}
    GET  /api/runs/<id>/export[?log=1]     reviewed, re-validated JSON (or the decision log)
    GET  /api/runs/<id>/source?file=&start=&end=   read-only excerpt of an original file
    DELETE /api/runs/<id>                  forget a finished run (and its uploaded files)
    POST /api/uploads                      -> {"id"}: start a staging folder
    PUT  /api/uploads/<id>?path=<rel>      upload one file into it (folder drag-and-drop)

Security: there is NO authentication. The server binds to 127.0.0.1 by default and can read any
local path you type in; do not expose it on a shared network.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

from ..config import PipelineConfig
from ..events import PipelineEvent
from ..pipeline import run_pipeline
from . import review, source

STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = PipelineConfig().max_file_bytes
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


class Run:
    """One pipeline execution plus its review state. Mutated only under ``self.cond``."""

    def __init__(self, source: str, label: str, threshold: float, spec: dict) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.source = source
        self.spec = spec  # the POST body that created this run, so the UI can re-run it
        self.input_path: Path | None = None  # set by State.start_run; used for source excerpts
        self.label = label
        self.threshold = threshold
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.status = "running"
        self.events: list[dict] = []
        self.document = None
        self.items: list[dict] = []
        self.decisions: dict[str, dict] = {}
        self.edits: dict[str, dict] = {}  # "dataset::column" -> {field: {"value", "original"}}
        self.exported = False
        self.error: str | None = None
        self.cond = threading.Condition()

    # EventSink protocol
    def emit(self, event: PipelineEvent) -> None:
        with self.cond:
            self.events.append(json.loads(event.model_dump_json()))
            self.cond.notify_all()

    def pending(self) -> int:
        return sum(1 for i in self.items if i["id"] not in self.decisions)

    def refresh_status(self) -> None:
        if self.status in ("running", "failed"):
            return
        self.status = "review" if self.pending() else ("exported" if self.exported else "ready")

    def brief(self) -> dict:
        doc = self.document
        return {
            "id": self.id, "source": self.source, "spec": self.spec, "label": self.label, "status": self.status,
            "created_at": self.created_at, "threshold": self.threshold, "error": self.error,
            "pending": self.pending(), "review_total": len(self.items), "edit_count": len(self.edits),
            "repository": doc.repository.name if doc else self.label,
            "title": doc.project.title if doc else None,
            "summary": doc.summary.model_dump() if doc else None,
            "duration_ms": doc.processing.duration_ms if doc else None,
        }

    def full(self) -> dict:
        data = self.brief()
        doc = review.preview(self.document, self.edits) if self.document else None
        data.update(events=self.events, items=self.items, decisions=self.decisions, edits=self.edits,
                    document=doc.model_dump(mode="json", by_alias=True) if doc else None)
        return data


class State:
    def __init__(self, samples_dir: Path) -> None:
        self.samples_dir = samples_dir
        self.runs: dict[str, Run] = {}
        self.uploads: dict[str, Path] = {}
        self.tmp = Path(tempfile.mkdtemp(prefix="rn-web-"))
        self.lock = threading.Lock()

    def samples(self) -> list[dict]:
        if not self.samples_dir.is_dir():
            return []
        return [{"name": p.name, "files": sorted(f.name for f in p.iterdir() if f.is_file())}
                for p in sorted(self.samples_dir.iterdir()) if p.is_dir()]

    def start_run(self, path: Path, source: str, label: str, threshold: float, spec: dict) -> Run:
        run = Run(source, label, threshold, spec)
        run.input_path = path
        with self.lock:
            self.runs[run.id] = run
        threading.Thread(target=self._execute, args=(run, path), daemon=True).start()
        return run

    def delete_run(self, run_id: str) -> None:
        """Forget a run; drop its upload folder once no other run (e.g. a re-run) still uses it."""
        with self.lock:
            run = self.runs.pop(run_id, None)
            shutil.rmtree(self.tmp / f"src-{run_id}", ignore_errors=True)
            upload = run.spec.get("upload") if run else None
            if upload and not any(r.spec.get("upload") == upload for r in self.runs.values()):
                folder = self.uploads.pop(upload, None)
                if folder is not None:
                    shutil.rmtree(folder, ignore_errors=True)

    @staticmethod
    def _execute(run: Run, path: Path) -> None:
        try:
            doc = run_pipeline(path, PipelineConfig(), run)
            items = review.review_items(doc, run.threshold)
            with run.cond:
                run.document, run.items, run.status = doc, items, "review"
                run.refresh_status()
        except Exception as exc:  # noqa: BLE001 - surface any crash to the UI instead of hanging
            with run.cond:
                run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            with run.cond:
                run.events.append({"stage": "_end", "status": run.status})
                run.cond.notify_all()


def _safe_relpath(raw: str) -> PurePosixPath:
    """Reject absolute paths and '..' so an upload cannot escape its staging folder."""
    rel = PurePosixPath(unquote(raw).replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(p in ("..", "") or ":" in p for p in rel.parts):
        raise ValueError(f"Unsafe upload path: {raw!r}")
    return rel


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "research-normalizer-web"

        def log_message(self, fmt, *args):  # quieter console
            pass

        # ---------------------------------------------------------------- helpers
        def _json(self, payload, status=HTTPStatus.OK, headers: dict | None = None):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, message):
            self._json({"error": message}, status)

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD_BYTES:
                raise ValueError("Request body too large.")
            return self.rfile.read(length) if length else b""

        def _run(self, run_id) -> Run | None:
            run = state.runs.get(run_id)
            if run is None:
                self._error(HTTPStatus.NOT_FOUND, "Run not found.")
            return run

        # ---------------------------------------------------------------- routing
        def do_GET(self):
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            query = parse_qs(url.query)
            if not parts or parts[0] != "api":
                return self._static(url.path)
            if parts == ["api", "samples"]:
                return self._json(state.samples())
            if parts == ["api", "runs"]:
                runs = sorted(state.runs.values(), key=lambda r: r.created_at, reverse=True)
                return self._json([r.brief() for r in runs])
            if len(parts) >= 3 and parts[1] == "runs":
                run = self._run(parts[2])
                if run is None:
                    return
                if len(parts) == 3:
                    with run.cond:
                        return self._json(run.full())
                if parts[3] == "events":
                    return self._sse(run)
                if parts[3] == "export":
                    return self._export(run, bool(query.get("log")))
                if parts[3] == "source":
                    return self._source(run, query)
            self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")

        def do_POST(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                if parts == ["api", "uploads"]:
                    uid = uuid.uuid4().hex[:8]
                    folder = state.tmp / uid
                    folder.mkdir()
                    state.uploads[uid] = folder
                    return self._json({"id": uid}, HTTPStatus.CREATED)
                body = json.loads(self._body() or b"{}")
                if parts == ["api", "runs"]:
                    return self._create_run(body)
                if len(parts) == 4 and parts[1] == "runs" and parts[3] == "decisions":
                    return self._decide(parts[2], body)
                if len(parts) == 4 and parts[1] == "runs" and parts[3] == "edits":
                    return self._edit(parts[2], body)
            except (ValueError, KeyError) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")

        def do_DELETE(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if len(parts) != 3 or parts[:2] != ["api", "runs"]:
                return self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")
            run = self._run(parts[2])
            if run is None:
                return
            with run.cond:
                if run.status == "running":
                    return self._error(HTTPStatus.CONFLICT, "Run is still processing; delete it once it finishes.")
            state.delete_run(run.id)
            self._json({"deleted": run.id})

        def do_PUT(self):
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            if len(parts) != 3 or parts[:2] != ["api", "uploads"] or parts[2] not in state.uploads:
                return self._error(HTTPStatus.NOT_FOUND, "Unknown upload.")
            try:
                rel = _safe_relpath(parse_qs(url.query).get("path", [""])[0])
                target = state.uploads[parts[2]] / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self._body())
            except ValueError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            self._json({"ok": True, "path": str(rel)})

        # ---------------------------------------------------------------- handlers
        def _create_run(self, body: dict):
            threshold = float(body.get("threshold", review.DEFAULT_THRESHOLD))
            if not 0.0 <= threshold <= 1.0:
                raise ValueError("threshold must be between 0 and 1.")
            if "sample" in body:
                names = {s["name"] for s in state.samples()}
                if body["sample"] not in names:
                    raise ValueError(f"Unknown sample: {body['sample']}")
                path, source, label = state.samples_dir / body["sample"], "sample", body["sample"]
            elif "upload" in body:
                folder = state.uploads.get(body["upload"])
                if folder is None:
                    raise ValueError("Unknown upload id.")
                entries = list(folder.iterdir())
                # A single uploaded root folder (or single .zip) is the repository itself.
                path = entries[0] if len(entries) == 1 else folder
                label = body.get("label") or path.name
                source = "upload"
            elif "path" in body:
                path = Path(str(body["path"]).strip().strip('"')).expanduser()
                if not path.exists():
                    raise ValueError(f"Path does not exist: {path}")
                source, label = "path", path.name
            else:
                raise ValueError("Provide 'sample', 'upload', or 'path'.")
            spec = {k: body[k] for k in ("sample", "upload", "path", "label") if k in body}
            run = state.start_run(path, source, label, threshold, spec)
            self._json(run.brief(), HTTPStatus.CREATED)

        def _decide(self, run_id: str, body: dict):
            run = self._run(run_id)
            if run is None:
                return
            with run.cond:
                item = next((i for i in run.items if i["id"] == body.get("item_id")), None)
                if item is None:
                    raise ValueError("Unknown review item.")
                decision = body.get("decision")
                if decision == "undo":
                    run.decisions.pop(item["id"], None)
                else:
                    review.validate_decision(item, decision)
                    run.decisions[item["id"]] = {
                        "decision": decision, "note": str(body.get("note") or "")[:500],
                        "decided_at": datetime.now(timezone.utc).isoformat(),
                    }
                run.exported = False
                run.refresh_status()
                return self._json(run.brief())

        def _edit(self, run_id: str, body: dict):
            run = self._run(run_id)
            if run is None:
                return
            with run.cond:
                if run.document is None:
                    raise ValueError("Run has no document yet.")
                dataset, column = str(body.get("dataset", "")), str(body.get("column", ""))
                key = f"{dataset}::{column}"
                if body.get("revert"):
                    review._find(run.document, dataset, column)
                    run.edits.pop(key, None)
                else:
                    fields = body.get("fields")
                    if not isinstance(fields, dict) or not fields:
                        raise ValueError("Provide 'fields' to change.")
                    cleaned = review.clean_edit(run.document, dataset, column, fields, run.edits.get(key, {}))
                    review.check_link_unique(run.document, run.edits, dataset, column, cleaned)
                    if cleaned:
                        run.edits[key] = cleaned
                    else:
                        run.edits.pop(key, None)
                review.sync_auto_decisions(run.items, run.decisions, run.edits)
                run.exported = False
                run.refresh_status()
                return self._json(run.brief())

        def _source(self, run: Run, query: dict):
            if run.document is None or run.input_path is None:
                return self._error(HTTPStatus.CONFLICT, "Run has no document yet.")
            try:
                rel = query.get("file", [""])[0]
                start = int(query.get("start", ["1"])[0])
                end = int(query["end"][0]) if "end" in query else None
                root = source.repository_root(run.input_path, state.tmp / f"src-{run.id}")
                return self._json(source.excerpt(run.document, root, rel, start, end))
            except ValueError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except FileNotFoundError as exc:
                return self._error(HTTPStatus.GONE, str(exc))

        def _export(self, run: Run, log: bool):
            with run.cond:
                if run.document is None:
                    return self._error(HTTPStatus.CONFLICT, "Run has no document yet.")
                if run.pending():
                    return self._error(HTTPStatus.CONFLICT, f"{run.pending()} review item(s) still pending.")
                if log:
                    payload = review.decision_log(run.id, run.items, run.decisions, run.edits)
                    name = f"{run.document.repository.name}.decisions.json"
                else:
                    doc = review.apply_decisions(run.document, run.items, run.decisions, run.edits)
                    payload = doc.model_dump(mode="json", by_alias=True)
                    name = f"{run.document.repository.name}.normalized.json"
                    run.exported = True
                    run.refresh_status()
            self._json(payload, headers={"Content-Disposition": f'attachment; filename="{name}"'})

        def _sse(self, run: Run):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            sent = 0
            try:
                while True:
                    with run.cond:
                        while sent >= len(run.events):
                            run.cond.wait(timeout=15)
                            if sent >= len(run.events):
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                        batch = run.events[sent:]
                    for ev in batch:
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                        sent += 1
                        if ev.get("stage") == "_end":
                            self.wfile.flush()
                            return
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        def _static(self, path: str):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC_DIR / rel).resolve()
            if STATIC_DIR.resolve() not in target.parents or not target.is_file():
                return self._error(HTTPStatus.NOT_FOUND, "Not found.")
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-normalizer-web", description="Local review dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost only).")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--samples", type=Path, default=Path("samples"), help="Folder of sample repositories.")
    args = parser.parse_args(argv)

    state = State(args.samples.resolve())
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    httpd.daemon_threads = True
    print(f"research-normalizer dashboard on http://{args.host}:{args.port}  (no auth; local use only)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        shutil.rmtree(state.tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
