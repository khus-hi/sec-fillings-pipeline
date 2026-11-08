# SEC Filings Extraction Pipeline

Turns unstructured 10-K filings from SEC EDGAR into reliable, queryable tables using a local
open-source LLM, with validation, retries, a quarantine table and a measured evaluation.

**Status:** Stage 1 of 9 (ingestion) built. See [PROGRESS.md](PROGRESS.md).

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

## Quick start (Stage 1)

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

_Chunking, validation and retries, and model choice are added in Stages 2 to 4._

## Failure modes I found

_Filled in from real runs._

## What I'd do at 10x scale

_Filled in at the end._
