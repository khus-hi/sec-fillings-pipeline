# Progress

Read this first in a new chat. It records what is done, the decisions behind it, and what's next.

## Owner's machine

_Not confirmed yet._ OS: ? | RAM: ? | GPU: ? | Python: ? | Docker: ?
Model pick depends on RAM: 8 GB → `qwen3:4b`, 16 GB → `qwen3:8b` (default), 32 GB+ → `qwen3:14b`.

## Stages

| # | Stage | Status |
|---|---|---|
| 1 | Ingestion: EDGAR → local S3 (SeaweedFS) raw zone, hashed manifest | **Built; real run done on owner's Mac; waiting on interview answers** |
| 2 | Parsing: sections + chunking | not started |
| 3 | Extraction schema (Pydantic) | not started |
| 4 | LLM extraction, retries, quarantine | not started |
| 5 | Evaluation on a 30-document gold set | not started |
| 6 | DuckDB + dbt staging → marts, tests | not started |
| 7 | Airflow DAG (Astro, as in Project 1) | not started |
| 8 | Optional: local vs hosted model comparison | not started |
| 9 | Streamlit app | not started |

## Stage 1: what exists

- `ingestion/edgar_client.py`: EDGAR client. Ticker → CIK, list 10-Ks (with history-page
  fallback for heavy filers like JPM), download. Rate limiter (5 req/s) + urllib3 retries.
- `ingestion/storage.py`: `S3Store` (SeaweedFS/AWS; auto-creates the bucket on a local endpoint) and `LocalStore`, same interface. `raw_key()`
  builds `raw/edgar/10-K/filing_date=YYYY-MM-DD/<cik>_<accession>.htm`.
- `ingestion/manifest.py`: DuckDB tables `meta.documents` (one row per filing, PK accession,
  sha256) and `meta.ingest_runs` (one row per run with counts and timing).
- `ingestion/ingest.py`: the CLI. `--tickers`, `--per-company`, `--limit`. Exit code 1 if anything failed.
- `ingestion/companies.csv`: 50 tickers across 11 sectors (5 each, except Materials 3 and Real Estate 2). Optional `ciks` column pins CIKs (XOM: new holdco + old entity).
- `docker-compose.yml`: SeaweedFS S3 server on :8333, file browser on :8888.
- `tests/`: 35 offline tests (fake EDGAR via `responses`, fake S3 via `moto`).

## Decisions and why

| Decision | Why | Alternative rejected |
|---|---|---|
| Partition by filing date | deterministic key → re-runs overwrite, never duplicate | ingestion date (re-run tomorrow = second copy) |
| Skip by accession before download | zero SEC calls on re-run | download then compare hash (wastes bandwidth, slower) |
| Store sha256 as document identity | later stages skip unchanged content | re-process everything each run |
| Object first, manifest second | crash leaves a re-downloadable orphan, not a dangling row | manifest first |
| Manifest in DuckDB | queryable, same warehouse as Stage 6 | JSON file in the bucket (no SQL, race-prone) |
| Exclude 10-K/A | amendments are partial, would double-count a year | include and dedupe later |
| 3 filings per company default | gives year-over-year view for the Stage 6 mart | 1 (no trend) |
| SeaweedFS in docker-compose (standalone for now) | S3 API locally at $0; MinIO was the first pick but its Docker images are no longer published (pull fails) | MinIO (unavailable), real S3 (costs, credentials); will fold into Astro's compose in Stage 7 |
| Project 1 patterns reused | `.env` config, store interface, ruff + pytest CI, pinned dev deps | — |

## Real-run metrics

Only numbers from the owner's real runs go here (source: `meta.ingest_runs`).

| Date | Run | Result |
|---|---|---|
| 2026-11-08 | first full ingest (MacBook Air, home network) | 147 10-Ks, 797.0 MB in 78.35 s (about 1.9 docs/s), 0 failed |
| 2026-11-08 | immediate re-run | 0 downloaded, 147 skipped, 18.39 s (index lookups only) |

Gap: 147, not 150, because XOM returned 0 filings (see README "Failure modes"). Fixed by pinning CIKs. Re-run to fetch XOM's 3.

## Next step

Owner runs Stage 1 on their machine (see README Quick start), confirms, answers the interview
questions, then says "go" for Stage 2 (parsing).
