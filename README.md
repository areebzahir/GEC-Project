# research-normalizer

Turn a research data repository — CSV, TSV, TAB and Excel files plus a README — into one
standardized, validated JSON document. The program reads the documentation, reads the data, works
out which README variable describes which data column, and produces a consistent JSON structure with
full provenance and clear warnings when something is uncertain.

Built for the 2026 GEC project. The full technical design and the reasoning behind every choice are
in [`docs/DESIGN.md`](docs/DESIGN.md).

## What it does

```
repository (dir / .zip / file)
      │
      ├─ discover & classify files        (README vs data vs other)
      ├─ read data files                  (detect encoding, delimiter, header, types)
      ├─ read the README(s)               (sections, variable definitions, metadata)
      ├─ match documented variables ↔ data columns   (with a confidence score)
      ├─ assemble one canonical structure
      └─ validate against a JSON Schema → write JSON
```

It is designed to work on repositories it has never seen before, not just the two examples it was
built with. It never crashes on one ugly file: problems become categorized **issues** in the output.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`

## Install

```bash
uv venv --python 3.13 .venv
uv pip install -e ".[dev]"      # drop [dev] if you only want to run it
```

## Usage

```bash
# Print JSON to stdout, progress to the terminal
research-normalizer samples/pig_decomposition_hawaii

# Write JSON to a file
research-normalizer samples/dairy_cattle_energy -o out.json

# A zip works too
research-normalizer my_repository.zip -o out.json

# Record the live processing events (for a future UI) as JSON Lines
research-normalizer my_repo -o out.json --events events.jsonl

# Reproducible output (no timestamps), and keep every row
research-normalizer my_repo -o out.json --deterministic --all-records
```

Flags: `-o/--output`, `--events`, `--config cfg.toml`, `--deterministic`, `--all-records`,
`-v/--verbose`. Exit codes: `0` success, `1` finished with recoverable errors, `2` fatal (no output).

### As a library

```python
from research_normalizer import run_pipeline
from research_normalizer.config import PipelineConfig

document = run_pipeline("path/to/repo", PipelineConfig())
print(document.summary)              # datasets, variables, matched, warnings …
json_text = document.model_dump_json(indent=2)
```

`run_pipeline` accepts an optional event sink, so a future GUI can show each stage live without any
changes to the backend.

## The output

One JSON document per repository. Top-level keys:

| Key | What it holds |
|---|---|
| `schema_version` | the format version (SemVer), currently `1.0.0` |
| `repository` | repository name and file count |
| `project` | title, description, authors/contacts, dates, funding, licence, methodology … |
| `documents` | each README parsed, which datasets it describes |
| `datasets` | one per data file / Excel sheet: structure, variables, typed `records` |
| `relationships` | datasets that share columns, and documented external links |
| `files` | every file found, its role, size, SHA-256 and status |
| `issues` | categorized problems (fatal / error / warning / info) with locations |
| `summary` | quick counts |
| `processing` | tool version, timings |

Each variable records its inferred `type`, `unit`, `value_labels`, column `statistics`, and a
`match` object showing how it was linked to the README (method, confidence, evidence, alternatives)
plus a `sources` map pointing back to the exact README lines. The canonical JSON Schema lives in
[`schemas/research_repository.schema.json`](schemas/research_repository.schema.json) and is generated
from the models with `python -m research_normalizer.schema`.

## Project layout

```
src/research_normalizer/
  config.py          every tunable value, with a rationale
  issues.py          categorized, explainable problems
  events.py          typed progress events (CLI now, GUI later)
  discovery.py       find & classify files, safe zip handling
  text_decoding.py   detect encoding (BOM → UTF-8 → detector → cp1252 → latin-1)
  schema/            the canonical output models + schema generator
  tabular/           read CSV/TSV/TAB/Excel; detect dialect, header, column types
  readme/            parse documentation into sections, variables, metadata
  matching/          link documented variables to data columns, with confidence
  linking.py         bind README sections to the data files they describe
  assemble.py        build the canonical datasets, relationships, project
  pipeline.py        run the stages in order and emit events  ← public entry point
  cli.py             command-line interface
tests/               unit tests per module + 20 end-to-end scenarios
benchmarks/          reproducible performance scripts + RESULTS.md
docs/                DESIGN.md (full design) and SOURCES.md (source log)
samples/             the two example Borealis repositories
```

## Technology choices (short version)

Python, because every heavy step already runs as compiled Rust/C++ inside a mature library, so the
code we write stays small and readable:

- **Polars** (Rust) — fast CSV/TSV reading and vectorized type inference
- **fastexcel / calamine** (Rust) — Excel reading; also safe (never evaluates formulas)
- **RapidFuzz** (C++) — fuzzy variable-name matching (~560x faster than `difflib` here)
- **Pydantic v2** (Rust core) — the schema, validation and JSON output
- **chardet**, **pypdf**, **python-docx** — encoding detection and PDF/Word READMEs

Full comparison, measurements and alternatives considered are in `docs/DESIGN.md` sections 4–7, and
numbers in `benchmarks/RESULTS.md`.

## Development

```bash
python -m pytest                       # run the test suite (54 tests)
python -m research_normalizer.schema --check   # verify the committed JSON Schema is current
python benchmarks/bench_pipeline.py    # end-to-end timing
```

## References

External sources that informed the design and implementation, in IEEE format. The running log with
what each one influenced is in [`docs/SOURCES.md`](docs/SOURCES.md). All URLs accessed 3 Oct. 2026.

[1] Polars Developers, "Excel — Polars user guide." https://docs.pola.rs/user-guide/io/excel/
[2] Polars Developers, "polars.read_csv — Polars documentation." https://docs.pola.rs/api/python/stable/reference/api/polars.read_csv.html
[3] ToucanToco, "fastexcel: A fast excel reader for Rust and Python," GitHub. https://github.com/ToucanToco/fastexcel
[4] J. Tuffé (tafia) et al., "calamine: A pure Rust Excel/OpenDocument SpreadSheets file reader," GitHub. https://github.com/tafia/calamine
[5] M. Bachmann, "RapidFuzz documentation," v3.14. https://rapidfuzz.github.io/RapidFuzz/
[6] Pydantic Services Inc., "JSON Schema — Pydantic documentation," v2.12. https://pydantic.dev/docs/validation/2.12/concepts/json_schema/
[7] D. Blanchard et al., "chardet: Python character encoding detector," GitHub. https://github.com/chardet/chardet
[8] A. R. Tahri (jawah), "charset_normalizer," GitHub. https://github.com/jawah/charset_normalizer
[9] pypdf Contributors, "Extract Text from a PDF — pypdf documentation." https://pypdf.readthedocs.io/en/latest/user/extract-text.html
[10] P. Holanda, "Automatic detection of types and dialects," DuckDB Blog, Oct. 27, 2023. https://duckdb.org/2023/10/27/csv-sniffer
[11] G. J. J. van den Burg, A. Nazábal, and C. Sutton, "Wrangling messy CSV files by detecting row and type patterns," *Data Mining and Knowledge Discovery*, vol. 33, no. 6, pp. 1799–1820, 2019, doi: 10.1007/s10618-019-00646-y.
[12] E. Rahm and P. A. Bernstein, "A survey of approaches to automatic schema matching," *The VLDB Journal*, vol. 10, no. 4, pp. 334–350, 2001, doi: 10.1007/s007780100057.
[13] Y. Shafranovich, "Common Format and MIME Type for Comma-Separated Values (CSV) Files," IETF RFC 4180, Oct. 2005. https://datatracker.ietf.org/doc/html/rfc4180
[14] P. Walsh and R. Pollock, "Table Schema," Frictionless Data Specifications, v1, 2021. https://specs.frictionlessdata.io/table-schema/
[15] Cornell Data Services, "Writing READMEs for research data," Cornell University. https://data.research.cornell.edu/content/readme/
[16] The Dataverse Project, "Tabular data, representation, storage and ingest," Dataverse User Guide. https://guides.dataverse.org/en/latest/user/tabulardataingest/ingestprocess.html
[17] DataCite Metadata Working Group, "DataCite Metadata Schema," v4.6, DataCite e.V., 2024. https://schema.datacite.org/
[18] OpenSSF Best Practices Working Group, "pyscg-0012: Excessive unzipping / zip slip," Secure Coding Guide for Python. https://best.openssf.org/Secure-Coding-Guide-for-Python/04_neutralization/pyscg-0012/
[19] WHATWG, "Server-sent events," HTML Living Standard. https://html.spec.whatwg.org/multipage/server-sent-events.html
[20] Python Software Foundation, "zipfile — Work with ZIP archives," Python 3 documentation. https://docs.python.org/3/library/zipfile.html
[21] UBC Library Research Commons, "Create a README," Research Data Management. https://ubc-library-rc.github.io/rdm/content/03_create_readme.html
[22] J. MacFarlane, "CommonMark Spec," v0.31.2, 2024; and "GitHub Flavored Markdown Spec." https://spec.commonmark.org/ and https://github.github.com/gfm/
[23] A. S. Schwartz and M. A. Hearst, "A simple algorithm for identifying abbreviation definitions in biomedical text," in *Proc. Pacific Symp. Biocomputing (PSB)*, vol. 8, 2003, pp. 451–462.
[24] W. W. Cohen, P. Ravikumar, and S. E. Fienberg, "A comparison of string distance metrics for name-matching tasks," in *Proc. IJCAI-03 Workshop on Information Integration on the Web (IIWeb)*, 2003, pp. 73–78.
[25] T. Döhmen, H. Mühleisen, and P. Boncz, "Multi-hypothesis CSV parsing," in *Proc. 29th Int. Conf. Scientific and Statistical Database Management (SSDBM)*, Chicago, IL, USA, Jun. 27–29, 2017, doi: 10.1145/3085504.3085520.
[26] C. Christodoulakis, E. B. Munson, M. Gabel, A. D. Brown, and R. J. Miller, "Pytheas: Pattern-based table discovery in CSV files," *Proc. VLDB Endowment*, vol. 13, no. 11, pp. 2075–2089, 2020, doi: 10.14778/3407790.3407810.
[27] J. Cant, P. Kedzierski, and C. Reyes, "Daily energy flows of freestall-housed dairy cattle estimated with automated data collection," Borealis, V1, 2026, doi: 10.5683/SP4/MS7XVT.
[28] E. L. Pecsi et al., "Biogeochemical changes in soils impacted by pig carcass decomposition in a Hawaiian tropical savanna ecosystem," Borealis, 2026, doi: 10.5683/SP4/UXYJGA.

### Dependency licences

Polars (MIT), fastexcel (MIT), calamine (MIT), RapidFuzz (MIT), Pydantic (MIT), chardet (0BSD),
pypdf (BSD-3-Clause), python-docx (MIT); dev: pytest (MIT), jsonschema (MIT), XlsxWriter (BSD).
No third-party source code was copied; algorithms adapted from papers [11][23][25][26] were
re-implemented and cited at the implementing function.
