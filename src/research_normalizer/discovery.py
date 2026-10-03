"""Find and classify the files in a research repository, safely.

Input can be a directory or a ``.zip``. We walk it without following symlinks, enforce size/count
limits, extract zips defensively against zip-slip and zip-bombs (DESIGN.md section 23, Ref [18][20]),
and classify each file as ``tabular``, ``documentation`` or ``other`` by extension plus a content
sniff (DESIGN.md section 10). Nothing is executed; nothing is silently ignored.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import (
    DELIMITED_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    EXCEL_EXTENSIONS,
    PipelineConfig,
)
from .issues import Issue, IssueCode, IssueCollector, Severity
from .text_decoding import decode_bytes, looks_like_text

# System/junk files that should never be treated as data or documentation.
_IGNORED_NAMES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_IGNORED_DIR_PARTS: frozenset[str] = frozenset({"__MACOSX", ".git", ".svn", "__pycache__"})

# Magic numbers used to confirm (or correct) the extension-based guess.
_XLSX_MAGIC = b"PK\x03\x04"          # xlsx/xlsm/xlsb/ods are zip containers
_XLS_MAGIC = b"\xd0\xcf\x11\xe0"      # legacy BIFF compound document
_PDF_MAGIC = b"%PDF"

# Documentation formats that are never data tables, so they skip the content sniff.
_ALWAYS_DOCUMENT_EXTENSIONS: frozenset[str] = (DOCUMENT_EXTENSIONS - DELIMITED_EXTENSIONS) | {".markdown"}
# Text extensions that promise a table (unlike .txt, which is as often prose).
_DATA_TEXT_EXTENSIONS: frozenset[str] = DELIMITED_EXTENSIONS - {".txt"}
# Text formats we recognise but do not parse (structured data, markup, code). Sniffing them as CSV
# would turn e.g. a JSON or SQL file into a nonsense table, so they are listed as "other".
_NON_TABULAR_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".json", ".geojson", ".jsonld", ".xml", ".html", ".htm", ".yaml", ".yml", ".toml", ".ipynb",
    ".svg", ".py", ".r", ".rmd", ".js", ".m", ".sh", ".sql", ".tex", ".bib", ".css",
})


class FileRole(StrEnum):
    TABULAR = "tabular"
    DOCUMENTATION = "documentation"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    """One file we found, with everything later stages need to decide what to do with it."""

    path: Path                 # absolute path on disk (possibly inside a temp dir for zips)
    relative_path: str         # path relative to the repository root, used in all output
    role: FileRole
    extension: str
    size_bytes: int
    sha256: str
    readme_score: float = 0.0  # higher = more likely to be the/a main README (documentation only)


@dataclass
class DiscoveryResult:
    """Outcome of discovery: the usable files, the repo name, and any temp dir to clean up."""

    repository_name: str
    files: list[DiscoveredFile]
    _cleanup: ExitStack = field(default_factory=ExitStack, repr=False)

    def close(self) -> None:
        """Remove any temporary directory created for a zip input."""
        self._cleanup.close()

    def __enter__(self) -> "DiscoveryResult":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _readme_score(name: str) -> float:
    """Rank documentation files by how README-like the name is (DESIGN.md section 10)."""
    low = name.lower()
    stem = Path(low).stem
    if "readme" in low or "read_me" in low:
        return 1.0
    if any(k in low for k in ("codebook", "data_dictionary", "data-dictionary", "dictionary")):
        return 0.9
    if any(k in low for k in ("metadata", "documentation", "methods", "methodology")):
        return 0.7
    if stem in ("manifest", "about", "info", "notes"):
        return 0.5
    return 0.2


def _classify(path: Path, head: bytes, config: PipelineConfig) -> FileRole:
    """Decide a file's role: binary magic first, then a content sniff for anything text-like.

    ``head`` is the first ``config.sample_bytes`` of the file. Extensions only route binary formats
    and break ties; for text the content decides (DESIGN.md section 10 step 6).
    """
    ext = path.suffix.lower()

    # Binary containers, recognised by magic bytes. A zip/OLE file is only a spreadsheet when the
    # extension agrees; otherwise it is some other container (.zip, .doc, .msg) we do not read.
    if head.startswith(_XLSX_MAGIC):
        if ext in EXCEL_EXTENSIONS:
            return FileRole.TABULAR
        return FileRole.DOCUMENTATION if ext == ".docx" else FileRole.OTHER
    if head.startswith(_XLS_MAGIC):
        return FileRole.TABULAR if ext in EXCEL_EXTENSIONS else FileRole.OTHER
    if head.startswith(_PDF_MAGIC):
        return FileRole.DOCUMENTATION

    # Known formats without the expected magic keep their extension role; their readers report a
    # corrupt file properly rather than us guessing here.
    if ext in EXCEL_EXTENSIONS:
        return FileRole.TABULAR
    if ext in _ALWAYS_DOCUMENT_EXTENSIONS:
        return FileRole.DOCUMENTATION
    if ext in _NON_TABULAR_TEXT_EXTENSIONS or not looks_like_text(head):
        return FileRole.OTHER

    return _classify_text(path, ext, head, config)


def _classify_text(path: Path, ext: str, head: bytes, config: PipelineConfig) -> FileRole:
    """Table or prose? Decided by the content; the README-ness of the name only breaks ties."""
    # Local import: tabular/__init__ imports base, which imports this module (import cycle).
    from .tabular.classify import EMBEDDED_LIST, tabular_evidence

    known_text = ext in DELIMITED_EXTENSIONS or ext == ""
    if not head.strip():
        # Empty file: nothing to sniff. Keep the extension's promise so the reader reports it.
        if ext in _DATA_TEXT_EXTENSIONS:
            return FileRole.TABULAR
        return FileRole.DOCUMENTATION if known_text else FileRole.OTHER
    evidence = tabular_evidence(decode_bytes(_cut_at_line(head), config).text)
    if evidence >= 1.0:
        return FileRole.TABULAR
    if evidence == EMBEDDED_LIST:
        # A list under document headings: data only if the extension says data and the name is not
        # a README ("measurements.csv" with a notes preamble), otherwise documentation.
        if ext in _DATA_TEXT_EXTENSIONS and _readme_score(path.name) < 0.5:
            return FileRole.TABULAR
        return FileRole.DOCUMENTATION
    if evidence > 0.0:  # consistent columns but only a couple of rows: let the name decide
        if _readme_score(path.name) >= 0.5:
            return FileRole.DOCUMENTATION
        return FileRole.TABULAR if known_text else FileRole.OTHER
    # Prose. Unknown extensions (.log, .R, ...) are not READMEs; keep them out of the parser.
    return FileRole.DOCUMENTATION if known_text else FileRole.OTHER


def _cut_at_line(head: bytes) -> bytes:
    """Drop a trailing partial line so a sample never ends mid-row or mid-character."""
    nl = head.rfind(b"\n")
    return head[: nl + 1] if nl > 0 else head


def _read_head(path: Path, n: int = 8) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def _is_safe_zip_member(name: str) -> bool:
    """Reject absolute paths, drive letters and parent-dir escapes (zip-slip, Ref [18])."""
    if name.startswith("/") or name.startswith("\\"):
        return False
    if len(name) >= 2 and name[1] == ":":  # Windows drive letter
        return False
    parts = Path(name.replace("\\", "/")).parts
    return ".." not in parts


def _extract_zip(zip_path: Path, dest: Path, config: PipelineConfig, issues: IssueCollector) -> None:
    """Safely extract a zip into ``dest`` with member, size and ratio guards (Ref [18][20])."""
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        if len(members) > config.max_zip_members:
            issues.add(Issue.make(
                IssueCode.ZIP_LIMIT_EXCEEDED, Severity.FATAL,
                "The archive contains too many files to process safely.",
                technical_detail=f"{len(members)} members > max_zip_members {config.max_zip_members}",
                file=zip_path.name,
            ))
            return
        for info in members:
            if info.is_dir():
                continue
            if not _is_safe_zip_member(info.filename):
                issues.add(Issue.make(
                    IssueCode.ZIP_UNSAFE_MEMBER, Severity.WARNING,
                    f"Skipped an unsafe path inside the archive: {info.filename!r}.",
                    technical_detail="Path is absolute or escapes the extraction root (zip-slip).",
                    file=zip_path.name,
                ))
                continue
            # Zip-bomb guards: cap total output and per-member compression ratio.
            total += info.file_size
            if total > config.max_zip_total_bytes:
                issues.add(Issue.make(
                    IssueCode.ZIP_LIMIT_EXCEEDED, Severity.FATAL,
                    "The archive expands to more data than the safety limit allows.",
                    technical_detail=f"uncompressed total exceeded {config.max_zip_total_bytes} bytes",
                    file=zip_path.name,
                ))
                return
            if info.compress_size > 0 and info.file_size / info.compress_size > config.max_zip_ratio:
                issues.add(Issue.make(
                    IssueCode.ZIP_UNSAFE_MEMBER, Severity.WARNING,
                    f"Skipped a suspiciously compressed file inside the archive: {info.filename!r}.",
                    technical_detail=(
                        f"ratio {info.file_size / info.compress_size:.0f} > max_zip_ratio "
                        f"{config.max_zip_ratio} (possible zip bomb)"
                    ),
                    file=zip_path.name,
                ))
                continue
            target = dest / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            # Stream the member out with a byte counter rather than trusting the header size.
            with zf.open(info) as src, target.open("wb") as out:
                written = 0
                while chunk := src.read(1024 * 1024):
                    written += len(chunk)
                    if written > config.max_zip_total_bytes:
                        break
                    out.write(chunk)


def discover(input_path: Path, config: PipelineConfig, issues: IssueCollector) -> DiscoveryResult | None:
    """Enumerate and classify the repository. Returns None on a fatal input problem.

    The caller must ``close()`` the result (or use it as a context manager) to clean up any temp dir.
    """
    input_path = input_path.resolve()
    if not input_path.exists():
        issues.add(Issue.make(
            IssueCode.INPUT_NOT_FOUND, Severity.FATAL,
            f"Input path does not exist: {input_path}",
            suggestion="Check the path you passed on the command line.",
        ))
        return None

    cleanup = ExitStack()
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        tmp = cleanup.enter_context(TemporaryDirectory(prefix="research_normalizer_"))
        root = Path(tmp)
        _extract_zip(input_path, root, config, issues)
        if issues.has_fatal:
            cleanup.close()
            return None
        repo_name = input_path.stem
    elif input_path.is_dir():
        root = input_path
        repo_name = input_path.name
    else:
        # A single data/README file is a valid (if unusual) repository of one.
        root = input_path.parent
        repo_name = input_path.stem

    only_file = input_path.name if (input_path.is_file() and input_path.suffix.lower() != ".zip") else None
    files = _walk_and_classify(root, repo_name, only_file, config, issues)

    if not files:
        issues.add(Issue.make(
            IssueCode.NO_FILES_FOUND, Severity.FATAL,
            "No supported files were found in the input.",
            technical_detail=f"root={root}",
            suggestion="The repository should contain CSV/TSV/TAB/Excel files and a README.",
        ))
        cleanup.close()
        return None

    return DiscoveryResult(repository_name=repo_name, files=files, _cleanup=cleanup)


def _walk_and_classify(
    root: Path,
    repo_name: str,
    only_file: str | None,
    config: PipelineConfig,
    issues: IssueCollector,
) -> list[DiscoveredFile]:
    found: list[DiscoveredFile] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune ignored directories in place so os.walk doesn't descend into them.
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_PARTS and not d.startswith(".")]
        for filename in sorted(filenames):
            if only_file is not None and filename != only_file:
                continue
            if filename in _IGNORED_NAMES or filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            if path.is_symlink() or not path.is_file():
                continue

            # Path containment guard: resolved path must stay under the root (DESIGN.md section 23).
            resolved = path.resolve()
            if not resolved.is_relative_to(root.resolve()):
                continue

            if len(found) >= config.max_files:
                issues.add(Issue.make(
                    IssueCode.TOO_MANY_FILES, Severity.WARNING,
                    "Stopped scanning: the repository has more files than the configured limit.",
                    technical_detail=f"max_files={config.max_files}",
                ))
                return found

            size = path.stat().st_size
            rel = str(path.relative_to(root))
            if size > config.max_file_bytes:
                issues.add(Issue.make(
                    IssueCode.FILE_TOO_LARGE, Severity.WARNING,
                    f"Skipped a file larger than the size limit: {rel}.",
                    technical_detail=f"{size} bytes > max_file_bytes {config.max_file_bytes}",
                    file=rel,
                ))
                continue

            role = _classify(path, _read_head(path, config.sample_bytes), config)
            found.append(DiscoveredFile(
                path=path,
                relative_path=rel,
                role=role,
                extension=path.suffix.lower(),
                size_bytes=size,
                sha256=_sha256(path),
                readme_score=_readme_score(filename) if role is FileRole.DOCUMENTATION else 0.0,
            ))

    # Deterministic order: documentation first (READMEs parsed before matching), then by path.
    found.sort(key=lambda f: (f.role is not FileRole.DOCUMENTATION, f.relative_path))
    return found
