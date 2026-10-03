"""Command-line entry point.

Usage:
    research-normalizer <input> [-o out.json] [--events events.jsonl] [--config cfg.toml]
                                [--deterministic] [--all-records] [-v]

Runs the pipeline, streams progress to the console, writes the standardized JSON, and prints a short
summary. Exit codes: 0 success, 1 completed with recoverable errors, 2 fatal (no output produced).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import PipelineConfig, load_config
from .events import ConsoleSink, JsonLinesSink, MultiSink
from .issues import Severity
from .pipeline import run_pipeline
from .schema.document import RepositoryDocument


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-normalizer",
        description="Convert a research data repository (CSV/TSV/TAB/Excel + README) into one "
                    "standardized JSON document.",
    )
    parser.add_argument("input", type=Path, help="A directory, a .zip, or a single data/README file.")
    parser.add_argument("-o", "--output", type=Path, help="Write JSON here (default: stdout).")
    parser.add_argument("--events", type=Path, help="Also write progress events as JSON Lines here.")
    parser.add_argument("--config", type=Path, help="Optional TOML configuration file.")
    parser.add_argument("--deterministic", action="store_true",
                        help="Omit timestamps/durations so output is byte-for-byte reproducible.")
    parser.add_argument("--all-records", action="store_true",
                        help="Emit every row (default caps records per dataset).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show technical detail per stage.")
    return parser


def _make_config(args: argparse.Namespace) -> PipelineConfig:
    config = load_config(args.config)
    overrides: dict[str, object] = {}
    if args.deterministic:
        overrides["deterministic"] = True
    if args.all_records:
        overrides["max_records_per_dataset"] = None
    return config.merged(**overrides)


def _exit_code(document: RepositoryDocument) -> int:
    severities = {i.severity for i in document.issues}
    if Severity.FATAL in severities:
        return 2
    if Severity.ERROR in severities:
        return 1
    return 0


def _print_summary(document: RepositoryDocument, stream) -> None:
    s = document.summary
    print("", file=stream)
    print(f"Repository : {document.repository.name}", file=stream)
    print(f"Datasets   : {s.datasets}", file=stream)
    print(f"Variables  : {s.matched}/{s.variables} matched"
          + (f", {s.undocumented} undocumented" if s.undocumented else ""), file=stream)
    print(f"Issues     : {s.warnings} warning(s), {s.errors} error(s)", file=stream)
    # List the first few actionable issues so the user knows what to check.
    actionable = [i for i in document.issues if i.severity in (Severity.ERROR, Severity.WARNING, Severity.FATAL)]
    for issue in actionable[:10]:
        where = f" [{issue.location.file}]" if issue.location and issue.location.file else ""
        print(f"  - {issue.severity.value}: {issue.message}{where}", file=stream)
    if len(actionable) > 10:
        print(f"  … and {len(actionable) - 10} more (see the JSON 'issues' array).", file=stream)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = _make_config(args)
    except (ValueError, OSError) as exc:
        print(f"error: could not load config: {exc}", file=sys.stderr)
        return 2

    # Console events go to stderr so stdout can carry pure JSON when no -o is given.
    sinks = [ConsoleSink(sys.stderr, verbose=args.verbose)]
    events_file = None
    if args.events:
        events_file = args.events.open("w", encoding="utf-8")
        sinks.append(JsonLinesSink(events_file))
    sink = MultiSink(*sinks)

    try:
        document = run_pipeline(args.input, config, sink)
    finally:
        if events_file is not None:
            events_file.close()

    payload = document.model_dump_json(by_alias=True, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        try:
            print(payload)
        except BrokenPipeError:
            # stdout was closed early (e.g. piped to `head`); not an error for us.
            return _exit_code(document)

    _print_summary(document, sys.stderr)
    return _exit_code(document)


if __name__ == "__main__":
    raise SystemExit(main())
