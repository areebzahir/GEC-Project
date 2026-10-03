# Process map

How research-normalizer works end to end: what runs, in what order, which file does what, and how the files depend on each other. For the reasoning behind each decision, see [DESIGN.md](DESIGN.md).

## 1. The big picture

```mermaid
flowchart LR
    IN["Research repository<br/>folder · .zip · single file<br/>data (CSV/TSV/TAB/Excel) + README"]
    PIPE["Pipeline<br/>pipeline.run_pipeline()"]
    DOC["RepositoryDocument<br/>one validated JSON document"]
    CLI["CLI<br/>research-normalizer"]
    WEB["Review dashboard<br/>research-normalizer-web"]
    OUT["out.json<br/>+ events.jsonl"]
    EXP["reviewed .normalized.json<br/>+ .decisions.json"]

    IN --> PIPE --> DOC
    CLI -- calls --> PIPE
    WEB -- calls --> PIPE
    DOC --> CLI --> OUT
    DOC --> WEB --> EXP
```

There are two ways in:

- **CLI** (`cli.py`): run once, write the JSON, print a summary.
- **Dashboard** (`web/`): run in the browser, watch the stages, review anything uncertain, fix variables, then export.

Both call the same `run_pipeline()`, so they always produce the same document.

## 2. The pipeline, stage by stage

`pipeline.py` is the only module that knows the stage order. Each stage reports progress as typed events (`events.py`) and records problems as issues (`issues.py`) instead of crashing. If one file breaks, the pipeline adds an `error` issue for it and moves on.

```mermaid
flowchart TD
    A["1 · Discovery<br/>discovery.py"] --> B["2 · Classification<br/>discovery.py"]
    B --> C["3 · Structure detection<br/>tabular/*"]
    C --> D["4 · README parsing<br/>readme/*"]
    D --> E["5 · Linking<br/>linking.py"]
    E --> F["6 · Variable matching + assembly<br/>assemble.py · matching/*"]
    F --> G["7 · Relationships + project<br/>assemble.py"]
    G --> H["8 · Validation + output<br/>schema/document.py"]

    C -. "real column names<br/>(helps find variable lists)" .-> D
```

| # | Stage | What happens | Main files | In → out |
|---|---|---|---|---|
| 1 | Discovery | Walk the folder, or extract the zip safely (zip-slip and zip-bomb guards). Skip hidden and system files, enforce size and count limits, record a sha256 per file. | `discovery.py` | path → `DiscoveryResult` |
| 2 | Classification | Tag each file as `documentation`, `tabular` or `other`, by extension plus a content sniff. Unsupported files are skipped with an info issue. | `discovery.py` | files → `DiscoveredFile.role` |
| 3 | Structure detection | For each data file: decode (encoding ladder), sniff the delimiter and quote character, find the real header row, read every cell as a string. Excel yields one table per sheet. | `tabular/base.py`, `delimited.py`, `excel.py`, `dialect.py`, `header_detection.py`, `text_decoding.py` | `DiscoveredFile` → `RawTable` |
| 4 | README parsing | Load the text (txt/md/pdf/docx), split it into sections and blocks, pull out project fields (title, people, license, dates), variable definitions in any of 4 layouts, and abbreviations. | `readme/parser.py`, `loaders.py`, `segmenter.py`, `project_fields.py`, `variable_layouts.py`, `abbreviations.py` | path → `ParsedReadme` |
| 5 | Linking | Bind each README variable list to the data file it names, by file stem (`X.csv` in the README matches `X.tab` on disk). | `linking.py` | groups → per-file groups + global groups |
| 6 | Matching + assembly | For each table: infer column types, profile the values, match columns to documented variables (5 scoring steps), build typed records, attach provenance. | `assemble.py`, `matching/*`, `tabular/type_inference.py`, `tabular/profiling.py` | `RawTable` + groups → `Dataset` |
| 7 | Relationships + project | Find datasets that share columns; merge project metadata from all READMEs. | `assemble.py` | datasets, READMEs → `relationships`, `Project` |
| 8 | Validation + output | Build the Pydantic `RepositoryDocument`, which validates as it is built, with a summary and per-stage timings. | `schema/document.py`, `pipeline.py` | everything → `RepositoryDocument` |

Data files are read **before** READMEs on purpose: knowing the real column names lets the README parser recognise a variable list even when it has no "Variable List" heading.

### What each stage hands to the next

```mermaid
flowchart LR
    P[Path] --> DR[DiscoveryResult<br/>files + roles + sha256]
    DR --> RT[RawTable<br/>string frame · headers · structure]
    DR --> PR[ParsedReadme<br/>sections · project fields<br/>variable groups · abbreviations]
    RT --> DS[Dataset<br/>variables · records · stats]
    PR --> LK[linked groups] --> DS
    PR --> PJ[Project]
    DS --> RD[RepositoryDocument]
    PJ --> RD
    IS[(IssueCollector)] --> RD
```

`IssueCollector` is passed through every stage, so the final document lists every problem with a severity, a plain message, a technical detail, a location and, where possible, a suggestion.

## 3. Inside the tricky stages

### 3a. Reading one data file (`tabular/delimited.py`)

```mermaid
flowchart TD
    B[raw bytes] --> E["decode_bytes()<br/>BOM → UTF-8 → chardet ≥0.80 → cp1252 → latin-1"]
    E --> S["sniff_dialect()<br/>try every delimiter × quote char,<br/>keep the most consistent parse"]
    S --> H["detect_header()<br/>score the first 30 rows:<br/>fill · textual · unique · contrast · README overlap"]
    H --> R{"Polars read"}
    R -- ok --> T[RawTable]
    R -- quote error --> R2["retry with quotes off<br/>CSV_QUOTE_FALLBACK"] --> T
    R2 -- still fails --> R3["stdlib csv, pad or trim ragged rows<br/>CSV_PARSE_FAILED"] --> T
```

Everything is read as strings. Missing values and types are decided later (`type_inference.py`), so a reader never silently turns `007` into `7` or `NA` into a number.

### 3b. Reading one README (`readme/parser.py`)

```mermaid
flowchart TD
    L["load_document()<br/>text / md / pdf / docx → numbered lines"] --> SG["segment()<br/>sections of typed blocks:<br/>heading · key/value · list · table row · rule · text"]
    SG --> PF["extract_project_fields()<br/>title · people · ORCID · license · dates · funding"]
    SG --> VG["_extract_variable_groups()<br/>walks sections, tracks the current data file,<br/>declared counts and missing codes"]
    VG --> VL["recognise_variables()<br/>block · delimited line · table · anchored<br/>the layout that finds the most definitions wins"]
    SG --> AB["extract_abbreviations()<br/>'Bacterial abundance (BA)' → BA = Bacterial abundance"]
    PF --> PR[ParsedReadme]
    VL --> PR
    AB --> PR
```

Every extracted value keeps its file and line range (`schema/provenance.py`). That's what the dashboard uses to highlight the exact README lines a description came from.

### 3c. Matching columns to documented variables (`matching/`)

```mermaid
flowchart TD
    P["every (column, documented name) pair"] --> S1{"1 exact"}
    S1 -- no --> S2{"2 case-insensitive / normalized<br/>Date ↔ date, milk-fat ↔ milk_fat"}
    S2 -- no --> S3{"3 token alias / abbreviation<br/>BWsmth_chg ↔ BWsmooth_chg"}
    S3 -- no --> S4["4 fuzzy · RapidFuzz Indel ratio × 0.90"]
    S1 & S2 & S3 & S4 --> C["5 context nudges<br/>same position · value labels · unit ⇒ numeric"]
    C --> G["greedy one-to-one assignment<br/>best free pair first, deterministic ties"]
    G --> M["VariableMatch per column<br/>status · confidence · method · evidence · alternatives"]
    G --> U["documented but no column<br/>→ unmatched_documented_variables"]
```

| Confidence | Meaning |
|---|---|
| ≥ 0.85 | accepted |
| 0.70 – 0.85 | kept, with a `MATCH_LOW_CONFIDENCE` warning |
| < 0.70 | not linked; the column is undocumented |
| best and runner-up within 0.05 | `MATCH_AMBIGUOUS` |

The dashboard adds its own review gate on top of this (0.95 by default), so even accepted-but-imperfect matches can be checked by a person.

## 4. The review dashboard

`research-normalizer-web` starts a local server (stdlib only, localhost, **no authentication**) that serves the frontend in `web/static/` and a small JSON API.

```mermaid
flowchart LR
    subgraph Browser["Browser · web/static/"]
        UI["app.js<br/>views · replay · review · drawer"]
    end
    subgraph Server["web/server.py"]
        API["HTTP handler<br/>/api/..."]
        RUN["Run objects (in memory)<br/>events · document · items · decisions · edits"]
        TH["pipeline thread"]
    end
    REV["web/review.py<br/>review gate · decisions · edits"]
    SRC["web/source.py<br/>README / raw data excerpts"]
    PIPE["pipeline.run_pipeline()"]

    UI <-- "JSON + SSE" --> API
    API --> RUN
    API --> TH --> PIPE
    PIPE -- events --> RUN
    API --> REV
    API --> SRC
```

### One run, from click to export

```mermaid
sequenceDiagram
    participant U as Browser (app.js)
    participant S as server.py
    participant T as pipeline thread
    participant R as review.py

    U->>S: POST /api/runs {sample | path | upload}
    S->>T: start run_pipeline() on a thread
    S-->>U: 201 run id (status: running)
    U->>S: GET /api/runs/<id>/events (SSE)
    T-->>S: PipelineEvent per stage
    S-->>U: data: {...} (streamed)
    T->>R: review_items(document, threshold)
    S-->>U: data: {"stage": "_end"}
    U->>S: GET /api/runs/<id>
    Note over U: replays the 6 stages at reading pace,<br/>then shows Overview + Review

    loop each flagged item / variable
        U->>S: POST /decisions {accept | reject | acknowledge | undo}
        U->>S: POST /edits {fields} or {revert}
        U->>S: GET /source?file=… (README lines, raw rows)
    end

    U->>S: GET /api/runs/<id>/export
    S->>R: apply_decisions(document, decisions, edits)
    R-->>S: copy, re-validated against the schema
    S-->>U: <repo>.normalized.json
```

**What goes into the review queue** (`review.review_items`):

| Kind | When | Allowed decisions |
|---|---|---|
| `low_confidence` | matched, but confidence below the dashboard threshold | accept · reject |
| `ambiguous` | two README variables fit almost equally | accept · reject |
| `undocumented_column` | column with no README description | acknowledge |
| `phantom_variable` | documented in the README, missing from the data (e.g. `FRESH`) | acknowledge |

**How changes are applied.** The pipeline's document is never modified. Decisions and edits are stored next to it:

- `preview(doc, edits)` is what the browser sees: the document plus edits.
- `apply_decisions(doc, items, decisions, edits)` builds the export: decisions first, then edits (a hand fix always wins), then the summary is recomputed and the result re-validated.
- Editing a flagged variable resolves its review item automatically (`sync_auto_decisions`). Reverting the edit reopens it.
- "Acknowledge N notices" (`acknowledge_all`) clears only the items whose sole option is acknowledge. Uncertain and ambiguous matches always wait for a person.

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/samples` | sample repositories in `./samples` |
| GET / POST | `/api/runs` | list runs / start one |
| GET / DELETE | `/api/runs/<id>` | full run / forget it (and its uploads) |
| GET | `/api/runs/<id>/events` | live progress (Server-Sent Events) |
| POST | `/api/runs/<id>/decisions` | record or undo a review decision |
| POST | `/api/runs/<id>/acknowledge-all` | acknowledge every pending notice; never accepts a match |
| POST | `/api/runs/<id>/edits` | correct a variable, or revert it |
| GET | `/api/runs/<id>/source` | read-only excerpt of an original file |
| GET | `/api/runs/<id>/export[?log=1]` | reviewed JSON, or the decision log |
| POST / PUT | `/api/uploads[/<id>?path=]` | stage dropped files for a run |

Runs live in memory and disappear when the server stops.
