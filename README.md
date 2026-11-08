# SEC Filings Extraction Pipeline

Turns unstructured 10-K filings from SEC EDGAR into reliable, queryable tables using a local
open-source LLM, with validation, retries, a quarantine table and a measured evaluation.

**Status:** Stages 1 (ingestion) and 2 (parsing) built. See [PROGRESS.md](PROGRESS.md).

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

_Filled in at Stage 3._

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
- **Chunks of whole blocks with overlap.** About 6,000 characters (~1,500 tokens) per chunk, built
  from whole paragraphs and table rows, with ~600 characters carried over so a fact on a boundary
  appears whole in one chunk. Sized in characters: no tokenizer dependency, same result everywhere.
- **Idempotent by content hash + parser version.** A filing is re-parsed only if its SHA-256 is new
  or `PARSER_VERSION` changed. Each document's rows are replaced in one transaction.

_Validation and retries, and model choice are added in Stages 3 and 4._

## Failure modes I found

| Stage | What happened | Fix |
|---|---|---|
| 1 | The first full run stored 147 of 150 filings with **no error**. ExxonMobil's ticker (XOM) had moved to a new holding company (EDGAR CIK 2115436, registered with an 8-K12B), whose filing history has no 10-K yet. The 10-K history stays under the old Exxon Mobil Corp CIK (34088). | A company that returns 0 filings now counts as a failure (the run exits non-zero). `companies.csv` can pin several CIKs per ticker, and filings from all of them are merged. |

## What I'd do at 10x scale

_Filled in at the end._
