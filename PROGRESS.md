# Progress

Project log: what is built, the decisions behind it, measured results, and what's next.

## Reference machine

All measured numbers come from one laptop: MacBook Air (Apple Silicon, 16 GB RAM, no discrete GPU,
fanless), macOS, Python 3.13, Docker Desktop. Local LLM: `qwen3:8b` via Ollama (chosen to fit
16 GB; 8 GB machines would use `qwen3:4b`, 32 GB+ `qwen3:14b`).

## Stages

| # | Stage | Status |
|---|---|---|
| 1 | Ingestion: EDGAR → local S3 (SeaweedFS) raw zone, hashed manifest | **Done** |
| 2 | Parsing: sections + chunking | **Done** (one known gap: SO Item 7) |
| 3 | Extraction schema (Pydantic) | **Done** |
| 4 | LLM extraction, retries, quarantine | **Ran on all 150 filings; reviewing suspicious values** |
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

## Stage 2: what exists

- `parsing/html_text.py`: iXBRL-aware HTML to text (hidden blocks skipped, tables as `|` rows,
  page furniture dropped).
- `parsing/sections.py`: finds Items 1, 1A, 7, 8; longest-candidate rule beats the table of
  contents. When an "Item N" section is short, falls back to section titles (MD&A, auditor's
  report, run-in "RISK FACTORS." headings); `parsed.sections.found_by` records which rule won.
- `parsing/chunking.py`: block-aligned chunks, 6,000 chars max, 600 chars overlap.
- `parsing/parse.py`: CLI; writes `parsed.documents`, `parsed.sections`, `parsed.chunks`,
  `meta.parse_runs`; skips by (sha256, PARSER_VERSION); per-document transaction.
- `parsing/report.py`: coverage per section (incl. how many found by title), short/missing
  documents, chunk sizes, runs.
- `parsing/inspect_doc.py`: debugging view for one filing (heading candidates, picked sections,
  lines that mention an Item or a section title).
- Tests: synthetic iXBRL 10-K with a table of contents, hidden header, page furniture and
  cross-references; chunking invariants; pipeline idempotency, version bump, rollback.

## Stage 3: what exists

- `extraction/schema.py`: Pydantic models. Three LLM tasks (`FinancialsExtraction`,
  `EmployeesExtraction`, `RiskFactorsExtraction`), the stored `FilingRecord` (manifest facts +
  answers + provenance, computed `fiscal_year`, `revenue_usd`, `net_income_usd`), 11 risk
  categories with definitions, `sanity_warnings()`, and `llm_json_schema()` (flat schema for
  Ollama). `python -m extraction.schema` prints the schemas.
- Quote fields are required (nullable) since schema version 2: on the first real run every
  optional quote came back empty.
- Tests: units and sign rules, explicit nulls, extra keys, category normalisation, list bounds and
  dedupe, manifest field formats, sanity warnings, schema flattening, Ollama request shape.

## Stage 4: what exists

- `extraction/context.py`: excerpt per task (employee-count lines, best income-statement chunk,
  Item 1A risk headings), capped at 6,000 characters.
- `extraction/prompts.py`: system prompt, task instructions, retry message; `PROMPT_VERSIONS`
  (one per task).
- `extraction/llm.py`: `LLMClient` protocol and `OllamaClient` (constrained JSON, `think=false`,
  temperature 0, 8k context; returns latency, tokens, cost).
- `extraction/extract.py`: the job. Retries with validation errors (3 attempts), quarantine,
  grounding warnings, record assembly, idempotent by (sha256, task, model, task version); a stored answer that newer validators
  reject counts as not done.
  Tables: `extracted.task_results`, `extracted.quarantine`, `extracted.filings`, `meta.extract_runs`.
- `extraction/report.py`: validity, retries, quarantine, latency, tokens, warnings, values.
- Tests: a scripted fake model drives retry, quarantine and recovery, skip and force, version bump,
  grounding warnings, server-down handling, missing period end.

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
| Manifest facts (CIK, period) never asked of the LLM | can't be hallucinated; fewer tokens | one big "extract everything" schema |
| Money as value + unit, converted in code | small models mis-scale numbers by 1,000x | ask for dollars directly |
| One schema per task (financials, employees, risks) | each call fits 8k context with its own section; partial success possible | single schema over the whole filing |
| Fixed list of 11 risk categories, near-miss aliases, no silent "other" | stable for the by-industry mart; drift stays visible | free-text categories (unaggregatable) |
| Excerpt chosen per task, not "first chunk with the keyword" | first real test fed JPM's model the wrong paragraph | send the whole section (doesn't fit 8k) or embeddings retrieval (another model to run) |
| Risk factors from Item 1A headings | whole list in ~1,500 tokens; the first chunk only covers one or two risks | first chunk; map-reduce over every chunk (10x the calls) |
| Retry with Pydantic errors, then quarantine | the model fixes most mistakes when told exactly what failed; failures stay visible | drop invalid answers; retry blindly |
| Grounding warnings, not rejections | a number missing from the excerpt is a strong hallucination signal, but quotes can be reformatted | reject (would quarantine honest reformatting) |
| Project 1 patterns reused | `.env` config, store interface, ruff + pytest CI, pinned dev deps | — |

## Real-run metrics

Only numbers from real runs go here (source: `meta.ingest_runs`).

| Date | Run | Result |
|---|---|---|
| 2026-11-08 | first full ingest (MacBook Air, home network) | 147 10-Ks, 797.0 MB in 78.35 s (about 1.9 docs/s), 0 failed |
| 2026-11-08 | immediate re-run | 0 downloaded, 147 skipped, 18.39 s (index lookups only) |
| 2026-11-08 | re-run after the XOM fix | 3 downloaded, 147 skipped, 0 failed, 20.13 s; **150 / 150 filings** |

The first run's gap (147, not 150) was XOM returning 0 filings; see README "Failure modes I found".

Stage 2 (source: `meta.parse_runs`, `python -m parsing.report`):

| Date | Run | Result |
|---|---|---|
| 2026-11-08 | parser v1, 150 filings | 150 parsed, 0 failed, 22.65 s (6.62 docs/s), 77.9 M characters of text |
| 2026-11-08 | parser v1 re-run | 0 parsed, 150 skipped |
| 2026-11-08 | parser v2 (title fallback) | 150 parsed, 0 failed, 27.23 s (5.51 docs/s); 12,916 chunks, avg 1,327-1,421 tokens, max 1,500 |

Short sections (< 2,000 chars, i.e. probably the wrong text), out of 150 filings:

| Section | v1 | v2 | v2 found by title |
|---|---|---|---|
| Item 1 | 9 | 1 | 8 |
| Item 1A | 9 | 0 | 9 |
| Item 7 | 24 | 3 | 21 |
| Item 8 | 39 | 0 | 39 |

Remaining: SO Item 7 (all 3 years, 1,245 chars) and GE's 2024 Item 1 (37 chars).

Stage 3 local-model check (`qwen3:8b`, one excerpt per task, AAPL and JPM): 6 / 6 answers valid
on the first attempt, 2.4-13.8 s per call, prompts up to 2,502 tokens. It also showed the problems
Stage 4 fixes: every optional quote empty, JPM's employee excerpt was the wrong paragraph, and the
first Item 1A chunk gives a shallow risk list.

Stage 4 (source: `meta.extract_runs`; `qwen3:8b` on the reference machine, $0):

| Date | Run | Result |
|---|---|---|
| 2026-11-08 | `--limit 3` (AAPL FY2024-2026) | 9 / 9 tasks valid on the first attempt, 0 retries, 0 quarantined, 0 warnings; 13,799 tokens; 81.82 s |
| 2026-11-08 | remaining 147 filings | 441 / 441 tasks valid, 0 retries, 0 quarantined; 644,660 tokens; 5,686.44 s (about 39 s per filing) |
| 2026-11-09 | financials re-run (prompt v2, new validators), 150 filings | 147 / 150 valid, 8 retries, 3 quarantined (NVDA FY2025, VZ FY2025, XOM FY2023); 300 tasks skipped from cache; 398,878 tokens; 2,347.58 s |

All 150 filings produced a record. From `python -m extraction.report` after that run: revenue
present in 143 / 150 records, net income 146, employee count 144; 33 records carry at least one
warning (28 net-income quotes and 21 revenue quotes not found verbatim in the excerpt, 6 employee
counts not found, 1 revenue value not found, 1 net income larger than revenue).

**Schema-valid is not the same as correct.** Reading the extracted values against the companies'
reported figures showed answers from the wrong row: XOM FY2023 and NVDA FY2025 with revenue equal
to net income, VZ FY2025 with a 99% net margin, WMT FY2024 revenue of $0.56B. Some revenue was
missing where the line is labelled "Sales" or "Sales to customers" (JNJ, MRK, NEM), and some
combined utility filings (AEP FY2023, D FY2024) returned a subsidiary's figures. Fixes (financials
prompt v2, margin and identical-amount validators, per-task cache keys) re-run only the
financials. Accuracy is measured properly against a gold set in Stage 5; these examples are from
review, not a metric.

After the financials re-run, the three filings that had answered with the wrong row now fail
validation three times and are quarantined instead of stored. That run exposed one more bug: their
old records stayed in `extracted.filings`. A document whose current answers are incomplete now has
its record removed. From the report before that fix (150 rows, the three stale ones included):
revenue present in 142, net income 146, employee count 144; 23 records carry at least one warning
(down from 33).

Still open from review: AEP FY2023 is a subsidiary's figures, MRK FY2023 revenue looks too low,
revenue is null for 8 filings, and two employee counts look like a single segment (HON FY2024,
OXY FY2023). Stage 5 measures these properly.

## Next step

Stage 5: the gold set and per-field metrics.
