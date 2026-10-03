# External sources log

Running record of every meaningful external source used in the design and implementation of this
backend. The README's IEEE References section is produced from this file. All URLs accessed
3 Oct. 2026 unless noted. No third-party source code was copied; algorithms adapted from papers are
re-implemented and cited at the implementing function (`# Ref [n]`).

| Ref | Source | What it influenced | Licence / note |
|---|---|---|---|
| [1] | Polars — Excel user guide (docs.pola.rs/user-guide/io/excel/) | Chose fastexcel/calamine engine; Arrow zero-copy handoff | lib MIT |
| [2] | Polars — `read_csv` API reference | Explicit `separator`/`quote_char`/`skip_rows`/`infer_schema=False` strategy; RFC 4180 note | lib MIT |
| [3] | fastexcel — GitHub (ToucanToco/fastexcel) | Excel reader: `header_row=None`, `dtypes`, `visible`, per-sheet reading | lib MIT |
| [4] | calamine — GitHub (tafia/calamine) | Underlying Rust reader; formulas not evaluated (security) | lib MIT |
| [5] | RapidFuzz docs v3.14 | Scorer choice (`fuzz.ratio` over Jaro-Winkler), `process.cdist` | lib MIT |
| [6] | Pydantic v2 — JSON Schema docs | Models as single source of truth; `model_json_schema`, `model_dump_json` | lib MIT |
| [7] | chardet 7 — GitHub | Encoding detector as one gated rung; accuracy/licence rationale | lib 0BSD |
| [8] | charset-normalizer — GitHub | Considered alternative detector; swap-in note | lib MIT |
| [9] | pypdf — Extract Text docs | PDF README reading with `extraction_mode="layout"` | lib BSD-3 |
| [10] | DuckDB CSV sniffer blog (2023-10-27) | Dialect/type/header detection phases; row-consistency scoring | blog |
| [11] | van den Burg, Nazábal, Sutton, "Wrangling messy CSV files…", DMKD 33(6):1799–1820, 2019 | Consistency-based dialect detection; messy-CSV test philosophy | paper, re-implemented |
| [12] | Rahm & Bernstein, "A survey of approaches to automatic schema matching", VLDB J. 10(4):334–350, 2001 | Multi-stage matcher taxonomy; schema- vs instance-level evidence | paper |
| [13] | RFC 4180 (Shafranovich, 2005) | CSV conformance expectations behind the fallback ladder | RFC |
| [14] | Frictionless Table Schema v1 | Field model; missingValues applied before type casting; type list | spec CC-BY |
| [15] | Cornell Data Services — Writing READMEs for research data | README section vocabulary; variable-list / missing-code conventions | CC0 template |
| [16] | Dataverse — Tabular data ingest guide | Why samples are `.tab` while READMEs name `.csv`; stem linking | docs |
| [17] | DataCite Metadata Schema v4.6 | Project field names (creators, title, description, funding, identifier) | spec CC0 |
| [18] | OpenSSF Secure Coding for Python — zip slip (pyscg-0012) | Safe zip extraction rules | guide CC-BY |
| [19] | WHATWG HTML Living Standard — Server-sent events | Event model shaped for future SSE forwarding (not implemented) | spec |
| [20] | Python docs — zipfile | Decompression-bomb and extractall pitfalls | docs |
| [21] | UBC Library — Create a README | Confirms Borealis README conventions in Canada | guide |
| [22] | CommonMark 0.31.2 + GFM tables spec | Markdown heading and pipe-table recognition | spec |
| [23] | Schwartz & Hearst, "A simple algorithm for identifying abbreviation definitions in biomedical text", PSB 8:451–462, 2003 | Abbreviation harvesting (`Long Form (LF)`) for matching aliases | paper, re-implemented |
| [24] | Cohen, Ravikumar, Fienberg, "A comparison of string distance metrics for name-matching tasks", IIWeb-03, 2003, pp. 73–78 | Why Jaro-Winkler's prefix bonus is risky for our codes | paper |
| [25] | Döhmen, Mühleisen, Boncz, "Multi-hypothesis CSV parsing", SSDBM '17, doi 10.1145/3085504.3085520 | Multi-hypothesis dialect ranking idea | paper, re-implemented |
| [26] | Christodoulakis et al., "Pytheas: pattern-based table discovery in CSV files", PVLDB 13(11):2075–2089, 2020 | Row-classification scoring behind header detection | paper, re-implemented |
| [27] | Cant, Kedzierski, Reyes, Borealis, doi 10.5683/SP4/MS7XVT | Sample repository #1 (dairy cattle energy) | CC BY 4.0 data |
| [28] | Pecsi et al., Borealis, doi 10.5683/SP4/UXYJGA | Sample repository #2 (pig decomposition) | CC BY 4.0 data |

Verification pass (3 Oct. 2026): bibliographic details for [11], [23], [24], [25] confirmed against
publisher/author pages (Springer; psb.stanford.edu; CMU/MIT author copies; ACM DL).
