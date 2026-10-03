# Research Normalizer

**Team name:** Amek
**Project title:** Research Normalizer: turning research data repositories into one standardized, validated JSON document

Research Normalizer is a Python tool that converts research data repositories into a standardized and validated JSON document.

It can process repositories containing CSV, TSV, TAB, Excel, and README files. The tool reads the documentation and data, matches documented variables to data columns, validates the results, and reports any issues it finds.

Built for the 2026 GEC project.

## Features

- Supports CSV, TSV, TAB, and Excel files
- Reads and analyzes README documentation
- Matches documented variables to data columns
- Detects and reports processing issues
- Validates output using JSON Schema
- Produces a standardized JSON document
- Supports ZIP repositories
- Provides processing events for future UI integration

## Requirements

- Python 3.12 or newer
- uv or pip

## Installation

Create a virtual environment:

    uv venv --python 3.13 .venv

Install the project:

    uv pip install -e ".[dev]"

## Usage

Process a research repository:

    research-normalizer samples/pig_decomposition_hawaii

Save the output to a JSON file:

    research-normalizer samples/dairy_cattle_energy -o out.json

Process a ZIP file:

    research-normalizer my_repository.zip -o out.json

For more options:

    research-normalizer --help

## Review Dashboard

A local web dashboard runs the same pipeline in the browser, shows each stage, and lets a person review uncertain matches, correct variables against the original README and data, and export the reviewed JSON:

    research-normalizer-web

Then open http://127.0.0.1:8765. It has no login and only listens on your own machine.

How all the pieces fit together is described in docs/PROCESS_MAP.md.

## Python Usage

The pipeline can also be used directly in Python:

    from research_normalizer import run_pipeline
    from research_normalizer.config import PipelineConfig

    document = run_pipeline("path/to/repository", PipelineConfig())

    print(document.summary)

## Output

The tool produces one standardized JSON document for each repository.

The output contains information about:

- The repository and project
- Documents and README files
- Datasets and variables
- Relationships between datasets
- Files and metadata
- Processing issues and warnings
- Processing summary

The JSON Schema used to validate the output is located in:

    schemas/research_repository.schema.json

## Project Structure

    src/research_normalizer/
    ├── config.py
    ├── issues.py
    ├── events.py
    ├── discovery.py
    ├── text_decoding.py
    ├── schema/
    ├── tabular/
    ├── readme/
    ├── matching/
    ├── linking.py
    ├── assemble.py
    ├── pipeline.py
    ├── cli.py
    └── web/            (review dashboard: server + static frontend)

    tests/
    benchmarks/
    docs/
    samples/

## Development

Run the tests:

    python -m pytest

Check the JSON Schema:

    python -m research_normalizer.schema --check

Run the benchmark:

    python benchmarks/bench_pipeline.py

## Documentation

More detailed information can be found in:

- docs/DESIGN.md — project design and architecture
- docs/PROCESS_MAP.md — diagrams of how the pipeline and dashboard work
- docs/SOURCES.md — research sources
- schemas/research_repository.schema.json — output schema