# SEC Filings Extraction Pipeline

Turns unstructured 10-K filings from SEC EDGAR into reliable, queryable tables using a local
open-source LLM, with validation, retries, a quarantine table and a measured evaluation.

**Status:** Stages 1 (ingestion), 2 (parsing) and 3 (extraction schema) built. See [PROGRESS.md](PROGRESS.md).

## Architecture

```mermaid
flowchart LR
    AF{{Airflow DAG}} -.orchestrates.-> I
    E[SEC EDGAR API] -->|ingestion/ingest.py<br/>rate limited, User-Agent| I[Raw zone<br/>SeaweedFS / S3<br/>raw/edgar/10-K/filing_date=YYYY-MM-DD/]
    I -->|sha256 + accession| M[(meta.documents<br/>manifest)]
    I -->|parsing/| P[Sections + chunks<br/>Item 1A, Item 7]
    P -->|extraction/<br/>Ollama, JSON schema| X{Pydantic<br/>validation}
    X -->|valid| W[(DuckDB raw tables)]
    X -->|retry, then fail| Q[(quarantine<br/>with reason)]
    W --> D[dbt staging → marts]
    D --> R[Risk categories<br/>by industry and year]
    D --> APP[Streamlit app]
    G[Gold set, 30 docs] --> EV[evaluation/<br/>precision, recall, F1]
    W --> EV
```

Boxes after the raw zone are planned and get built stage by stage.

## Quick start

Run these one at a time. (macOS ships `python3`, not `python`; inside the activated venv both work.)

1. Create `.env` and put your name and email in `SEC_USER_AGENT`:
   ```bash
   cp .env.example .env
   ```
2. Install:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements-dev.txt
   pytest -q
   ```
3. Start the local S3 server (file browser at http://localhost:8888):
   ```bash
   docker compose up -d
   ```
4. Ingest, then run again to prove idempotency (the second run downloads 0):
   ```bash
   python -m ingestion.ingest --tickers AAPL JPM --per-company 1
   python -m ingestion.ingest
   python -m ingestion.ingest
   python -m ingestion.summary
   ```
5. Parse every filing into sections and chunks, then check coverage:
   ```bash
   python -m parsing.parse
   python -m parsing.report
   ```
6. Check the local model can fill each extraction schema (needs Ollama running with `qwen3:8b`):
   ```bash
   python -m extraction.schema
   python -m extraction.try_schema AAPL
   ```

## Repository layout

| Folder | Stage | Contents |
|---|---|---|
| `ingestion/` | 1 | EDGAR client, raw-zone storage, manifest, company list |
| `parsing/` | 2 | HTML to text, section finder, chunking |
| `extraction/` | 3, 4 | Pydantic schema, LLM client, retries, quarantine |
| `evaluation/` | 5 | gold labels, metrics |
| `dbt/` | 6 | staging and marts on DuckDB |
| `dags/` | 7 | Airflow DAG |
| `app/` | 9 | Streamlit app |
| `tests/` | all | pytest, fully offline |

## Extraction schema

Defined in [`extraction/schema.py`](extraction/schema.py) with Pydantic. The model answers three
small tasks, each constrained to its own JSON schema; everything else comes from the manifest.

| Field | Type | Source | Rules |
|---|---|---|---|
| `cik` | 10-digit string | manifest | zero-padded |
| `ticker`, `company_name` | string | manifest | |
| `period_end`, `fiscal_year` | date, int | manifest (`reportDate`) | fiscal year = year the period ends |
| `revenue` | value + unit (+ quote) | LLM, Item 8 / 7 | value > 0; unit in units / thousands / millions / billions; `revenue_usd` computed in code |
| `net_income` | value + unit (+ quote) | LLM, Item 8 / 7 | may be negative; `net_income_usd` computed in code |
| `employee_count` | int (+ quote) | LLM, Item 1 | 1 to 5,000,000 |
| `risk_factors` | 1-8 × (title, category) | LLM, Item 1A | category from a fixed list of 11; duplicates dropped |
| `model`, `parser_version`, `schema_version`, `extracted_at` | provenance | pipeline | |

Risk categories: `market_competition`, `macroeconomic`, `regulatory_legal`,
`operational_supply_chain`, `cybersecurity_technology`, `financial_liquidity_credit`,
`geopolitical`, `climate_environmental`, `human_capital`, `strategic_reputational`, `other`.
Each has a one-line definition (`CATEGORY_DESCRIPTIONS`) shared by the prompt and the gold-set
labelling guide, so the model and the labels use the same rules.

Missing values must be an explicit `null`: a dropped key fails validation, so "not found" is
always a decision, never an accident. `sanity_warnings()` flags valid-but-suspicious records
(revenue under $1M or over $2T, net income larger than revenue, fewer than 1,000 employees) for
review instead of rejecting them.

## Evaluation results

_Filled in at Stage 5 from a real run. No numbers are written here until they are measured._

## Design decisions

**Ingestion (Stage 1)**
- **Partition by filing date, not download date.** The same filing always lands at the same key,
  so a re-run on a different day overwrites rather than duplicates.
- **Two idempotency checks.** The accession number (EDGAR's unique filing id) is checked before
  downloading, so re-runs cost zero SEC requests. The SHA-256 of the bytes is stored and becomes the
  document's identity for every later stage, so unchanged documents are never re-parsed or
  re-sent to the LLM.
- **Write the object first, then the manifest row.** A crash in between leaves an object with no
  row; the next run re-downloads it to the same key. The reverse order could leave a row pointing
  at nothing.
- **10-K only, no 10-K/A.** Amendments are often partial (only Part III) and would double-count a
  fiscal year.
- **Polite client.** 5 requests/second (SEC's limit is 10), retries with exponential backoff on
  429 and 5xx, and a required contact email in the User-Agent.

**Parsing (Stage 2)**
- **Own HTML-to-text walker on lxml, not "strip all tags".** 10-Ks are inline XBRL: a hidden
  `<ix:header>` block holds megabytes of machine data. The walker skips anything with
  `display:none`, keeps `<ix:nonFraction>` numbers inline, turns table rows into `a | b | c` lines,
  and drops page numbers and "Table of Contents" links.
- **Sections by heading, longest candidate wins.** Each Item heading appears in the table of
  contents and again at the real section. A heading must start a short line ("see Item 1A" mid-sentence
  doesn't count), and for each Item the candidate producing the longest span is kept, so the table
  of contents always loses. Sections under 2,000 characters are flagged as short (often
  "incorporated by reference" to an exhibit) rather than silently accepted.
- **Title fallback for short sections.** When the "Item N" section is short, the parser also looks
  for the section's title ("Management's Discussion and Analysis", "Report of Independent
  Registered Public Accounting Firm", run-in headings like "RISK FACTORS. The following ...") and
  keeps the longer span. Each section records whether it was found by heading or by title, so the
  fallback's reach is measurable (39 of 150 Item 8 sections).
- **Chunks of whole blocks with overlap.** About 6,000 characters (~1,500 tokens) per chunk, built
  from whole paragraphs and table rows, with ~600 characters carried over so a fact on a boundary
  appears whole in one chunk. Sized in characters: no tokenizer dependency, same result everywhere.
- **Idempotent by content hash + parser version.** A filing is re-parsed only if its SHA-256 is new
  or `PARSER_VERSION` changed. Each document's rows are replaced in one transaction.

**Extraction schema (Stage 3)**
- **Don't ask the model what you already know.** CIK, ticker, company and fiscal period come from
  EDGAR metadata. The model only reads what has to be read, so it can't hallucinate a CIK.
- **Money as printed, converted in code.** The model copies `391,035` and the table's unit
  (`millions`); code multiplies. Small models are unreliable at scaling numbers, and a 1,000x error
  still looks like a plausible number.
- **Three small schemas, not one big one.** Each task gets only its section's chunks, which keeps
  a call inside an 8k context on a laptop, and a failure in one task doesn't discard the others.
- **Strict, but forgiving where it's safe.** Unknown keys and unknown categories fail (so Stage 4
  can retry with the error message), while harmless near-misses (`"Supply Chain"`, `"CYBER"`) are
  normalised. Unknown labels are never silently mapped to `other`, which would hide drift.
- **Flat JSON schema for constrained decoding.** `$ref`s are inlined before the schema is passed
  to Ollama's `format=`, with field descriptions kept, so the schema doubles as instructions.

_Retries, quarantine and model choice are added in Stage 4._

## Failure modes I found

| Stage | What happened | Fix |
|---|---|---|
| 1 | The first full run stored 147 of 150 filings with **no error**. ExxonMobil's ticker (XOM) had moved to a new holding company (EDGAR CIK 2115436, registered with an 8-K12B), whose filing history has no 10-K yet. The 10-K history stays under the old Exxon Mobil Corp CIK (34088). | A company that returns 0 filings now counts as a failure (the run exits non-zero). `companies.csv` can pin several CIKs per ticker, and filings from all of them are merged. |

| 2 | On the first real parse, 39 of 150 Item 8 sections and 24 Item 7 sections were under 2,000 characters. Three layouts caused it: (1) "Item 8" is a one-line pointer and the statements sit in F-pages after Item 15 (NFLX, NVDA, ORCL, PEP, DIS, AMT, PLD); (2) "Item 7" says "incorporated by reference" and MD&A appears later under its own title (JPM, CVX, XOM, AEP); (3) GE, HON and MCD use an annual-report layout: the Item headings exist only in a cross-reference index at the end, and the body uses run-in headings in a different order (GE puts MD&A before Risk Factors). | Title-based fallback for short sections, ending at the next heading or title of any other Item (except "Properties" and "Legal Proceedings", which recur inside the notes). Short sections fell from 9 / 9 / 24 / 39 to 1 / 0 / 3 / 0 (Items 1 / 1A / 7 / 8). A `found_by` column makes the fallback auditable. |

## What I'd do at 10x scale

_Filled in at the end._
