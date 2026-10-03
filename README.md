# Research Normalizer

A tool for converting research data repositories into a standardized, validated JSON document.

The tool accepts repositories containing CSV, TSV, TAB, Excel, and README files. It analyzes the documentation and data, matches documented variables to data columns, validates the result, and produces a consistent JSON structure with provenance and warnings.

Built for the 2026 GEC project.

## What It Does

The Research Normalizer processes a research repository through the following stages:

1. Discovers and classifies files
2. Reads CSV, TSV, TAB, and Excel data
3. Parses README documentation and metadata
4. Matches documented variables to data columns
5. Builds a canonical repository structure
6. Validates the result against a JSON Schema
7. Produces a standardized JSON document

The tool is designed to work with repositories it has not seen before. Problems encountered during processing are reported as categorized issues instead of causing the entire pipeline to fail.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) (recommended) or `pip`

## Installation

Create a virtual environment and install the project:

    uv venv --python 3.13 .venv
    uv pip install -e ".[dev]"

If you only need to run the application, install it without the development dependencies:

    uv pip install -e .

## Usage

Process a research repository:

    research-normalizer samples/pig_decomposition_hawaii

Write the output to a JSON file:

    research-normalizer samples/dairy_cattle_energy -o out.json

Process a ZIP archive:

    research-normalizer my_repository.zip -o out.json

Record processing events:

    research-normalizer my_repo -o out.json --events events.jsonl

Create reproducible output and keep every record:

    research-normalizer my_repo -o out.json --deterministic --all-records

### Command-Line Options

| Option | Description |
|---|---|
| `-o`, `--output` | Write the resulting JSON to a file |
| `--events` | Record processing events as JSON Lines |
| `--config` | Use a custom configuration file |
| `--deterministic` | Produce reproducible output without timestamps |
| `--all-records` | Keep every input record |
| `-v`, `--verbose` | Enable verbose output |

Exit codes:

- `0` — Successful processing
- `1` — Completed with recoverable errors
- `2` — Fatal error; no output produced

## Python API

The pipeline can also be used directly from Python:

    from research_normalizer import run_pipeline
    from research_normalizer.config import PipelineConfig

    document = run_pipeline("path/to/repository", PipelineConfig())

    print(document.summary)

    json_text = document.model_dump_json(indent=2)

The pipeline also supports an optional event sink, allowing future interfaces to display processing progress.

## Output

The tool produces one JSON document for each processed repository.

The main sections include:

| Section | Description |
|---|---|
| `schema_version` | Version of the output format |
| `repository` | Repository name and file information |
| `project` | Project metadata such as title, authors, dates, and methodology |
| `documents` | Parsed README and documentation information |
| `datasets` | Data files, sheets, variables, and records |
| `relationships` | Relationships between datasets and documented links |
| `files` | Files discovered during processing |
| `issues` | Fatal errors, errors, warnings, and informational messages |
| `summary` | Processing and dataset statistics |
| `processing` | Tool version and processing information |

Each variable can include its inferred type, unit, labels, statistics, matching information, and source references.

The canonical JSON Schema is located at:

    schemas/research_repository.schema.json

## Project Structure

    src/research_normalizer/
    ├── config.py          Configuration and tunable values
    ├── issues.py          Categorized processing issues
    ├── events.py          Processing progress events
    ├── discovery.py       File discovery and classification
    ├── text_decoding.py   Text encoding detection
    ├── schema/            Output models and schema generation
    ├── tabular/           CSV, TSV, TAB, and Excel processing
    ├── readme/            README parsing
    ├── matching/          Variable-to-column matching
    ├── linking.py         README-to-data relationships
    ├── assemble.py        Canonical dataset construction
    ├── pipeline.py        Main processing pipeline
    └── cli.py             Command-line interface

    tests/                  Unit and end-to-end tests
    benchmarks/             Performance benchmarks
    docs/                   Design documentation and source references
    samples/                Example research repositories

## Technology

The project uses:

- **Polars** — tabular data processing
- **fastexcel / calamine** — Excel file processing
- **RapidFuzz** — variable-name matching
- **Pydantic** — validation and JSON models
- **chardet** — text encoding detection
- **pypdf** — PDF documentation processing
- **python-docx** — Word document processing

For detailed technology comparisons, design decisions, measurements, and alternatives, see:

[docs/DESIGN.md](docs/DESIGN.md)

## Development

Run the test suite:

    python -m pytest

Verify that the committed JSON Schema is current:

    python -m research_normalizer.schema --check

Run the end-to-end benchmark:

    python benchmarks/bench_pipeline.py

## Documentation

Additional project documentation:

- [docs/DESIGN.md](docs/DESIGN.md) — technical design and architecture
- [docs/SOURCES.md](docs/SOURCES.md) — research sources and references
- [schemas/research_repository.schema.json](schemas/research_repository.schema.json) — canonical JSON Schema

## License and Dependencies

The project uses third-party dependencies under their respective licenses.

Key dependencies include:

- Polars — MIT
- fastexcel — MIT
- calamine — MIT
- RapidFuzz — MIT
- Pydantic — MIT
- chardet — 0BSD
- pypdf — BSD-3-Clause
- python-docx — MIT

No third-party source code was copied into this project. Algorithms adapted from published research are re-implemented and cited in the relevant source code.

For the complete source list and dependency information, see [docs/SOURCES.md](docs/SOURCES.md).