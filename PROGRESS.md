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
| 4 | LLM extraction, retries, quarantine | **Done** (150 filings; 3 quarantined) |
| 5 | Evaluation: hand-checked gold set (32 filings) + XBRL reference (150) | **Done** |
| 6 | DuckDB + dbt staging → marts, tests | **Done** |
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

## Stage 5: what exists

- `evaluation/xbrl.py`: reads `<ix:nonFraction>` facts and their contexts from the raw filing;
  revenue (the largest registrant revenue fact, or revenue net of interest expense for banks)
  and net income for the registrant's fiscal year (no dimensions, 340 to 380 day duration
  ending at the period end), employee count when tagged. `evaluation/inspect_xbrl.py` lists
  the candidate facts for one filing.
- `evaluation/gold.py`: samples 32 filings (3 per sector, 2 for Real Estate, seed 2026), writes `gold_set.csv`
  pre-filled from XBRL with employee evidence sentences, and `risk_labels.csv` (titles only).
  XBRL values are cached in `eval.xbrl_reference` by content hash and XBRL-reader version.
- `evaluation/metrics.py`: precision, recall, F1 and exact match with null as an answer; 0.5%
  numeric tolerance; one-vs-rest label scores and macro F1.
- `evaluation/evaluate.py`: scores every model in `extracted.filings` against both references,
  stores `eval.runs`, `eval.field_scores`, `eval.mismatches`, prints a Markdown table.
- `evaluation/LABELLING.md`: labelling rules and the category definitions the prompt also uses.
- Tests: XBRL context selection (prior year, subsidiary, quarter ignored), value formats (scale,
  sign, dashes, comma decimals), scoring rules, reproducible sampling, end-to-end scoring.

## Stage 6: what exists

- `dbt/`: dbt-duckdb project on the same DuckDB file (`dbt/profiles.yml`, no secrets;
  `DBT_DUCKDB_PATH` overrides the file).
  - Staging (views): `stg_documents`, `stg_filings`, `stg_risk_factors` (risk list unnested,
    with rank), `stg_task_results` (with quarantine reason).
  - Marts (tables): `dim_companies`, `fct_filings` (net margin, revenue per employee,
    year-over-year growth), `fct_risk_factors`, `mart_risk_trends`, `mart_model_summary`.
  - Seed `risk_categories` (category, label, definition).
  - Tests: generic `unique_combination` and `between` macros, relationships, accepted values,
    and two singular tests (records match ok tasks; trends count every filing once).
- `tests/test_dbt.py`: builds the project on a sample warehouse made with the real DDL and
  checks the computed values, that a duplicate company-year fails the build, and that the seed
  matches `CATEGORY_DESCRIPTIONS`.

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
| Inline XBRL as reference for revenue and net income | free, covers all 150 filings, independent of the LLM and parser | hand-label all financials (slow); EDGAR companyfacts API (network call per company, frames by period not by filing) |
| Gold rows count only when verified | a pre-fill is a guess until a person checks it | score against raw pre-fills |
| Wrong value = FP and FN; quarantine = null prediction | wrong numbers can't hide behind "precision"; failures stay in the denominator | score only filings with a record |
| dbt for everything derived, Python for extraction | set-based logic in tested, versioned SQL; one DuckDB file, nothing to sync | ratios and aggregates in pandas (untested, rebuilt by hand) |
| Grain test on every model | a fan-out join double-counts silently; a uniqueness test fails the build | trust the joins |
| Risk trends include zero rows | charts and year-over-year comparisons never drop a category | only categories that occur |
| dbt built inside pytest in CI | one CI job, the sample warehouse uses the Python DDL so schema drift fails fast | separate dbt job with a hand-written fixture |
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

Stage 5 (source: `eval.field_scores`; qwen3:8b, financials prompt v2, 150 filings, XBRL
reference, no gold rows yet):

| Date | XBRL reader | Revenue P / R / F1 / exact | Net income P / R / F1 / exact |
|---|---|---|---|
| 2026-11-10 | v1 (first concept found) | 0.84 / 0.78 / 0.81 / 0.78 | 0.75 / 0.71 / 0.73 / 0.65 |
| 2026-11-10 | v2 (largest registrant revenue fact) | 0.89 / 0.83 / 0.86 / 0.81 | 0.75 / 0.71 / 0.73 / 0.65 |
| 2026-11-10 | v3 (net income fallback order), financials prompt v3 | 0.92 / 0.85 / 0.89 / 0.84 | 0.84 / 0.79 / 0.82 / 0.74 |

The first run scored the reference as much as the model: AXP, DUK and NEE were right and the
reader was wrong. After the fix, the mismatches split into (1) a systematic model error: 22 of the
first 40 net-income mismatches are consolidated net income including noncontrolling interests
(PG, PEP, UNH, SLB, PLD, TSLA, DUK, BA, HON) instead of the amount attributable to the company,
fixed in financials prompt v3; (2) wrong rows or years (MRK, NEM, GS 2024, VZ 2024, DIS 2023);
(3) nulls and the 3 quarantined filings; (4) a definition question for oil companies (CVX, COP,
OXY): sales and operating revenues or total revenues including other income. The gold set
settles (4).

Prompt v3 run (2026-11-10: 150 filings, financials only, 145 valid, 5 quarantined, 12 retries,
418,099 tokens, 2,141.49 s) raised net-income F1 from 0.73 to 0.82. The two new quarantines (D
FY2024: negative revenue; WMT FY2023: net income 2,760% of revenue) previously had a null
revenue, so they were already misses. About 14 net-income mismatches are still consolidated
figures (UNH, TSLA, PFE, DOW, AMT, AEP, BA); a prompt alone does not fully fix this for an 8B
model. One known reference error remains: for OXY, which has preferred stock, the reader falls
back to net income available to common stockholders.

Gold set labelled (2026-11-10): 32 rows verified, 80 risk titles categorised. Revenue and net
income checked against each income statement; 3 revenue pre-fills corrected (COP 2024 and OXY
2023 to total revenues and other income, D 2025 to total operating revenue, where the largest
XBRL fact was a contract-revenue subtotal). Employee counts looked up in each filing (11 were
missing from the evidence sentences, e.g. GS reports "headcount", NEE's total is in Item 1's
overview, FPL's figure is a subsidiary). Risk categories drafted with a different LLM, blind to
the model, reviewed by hand (MRK "Loss of Market Exclusivity" moved to market_competition).

First scored run with gold labels (run `10a10389216d`, qwen3:8b, financials prompt v3):

| Reference | Field | n | P | R | F1 | Exact |
|---|---|---:|---:|---:|---:|---:|
| gold | revenue_usd | 32 | 0.94 | 0.91 | 0.92 | 0.91 |
| gold | net_income_usd | 32 | 0.74 | 0.72 | 0.73 | 0.69 |
| gold | employee_count | 32 | 0.97 | 0.91 | 0.94 | 0.91 |
| gold | risk_category | 80 | 0.84 | 0.84 | 0.84 | 0.84 |
| gold | risk_category, macro F1 | | | | 0.86 | |
| xbrl | revenue_usd | 150 | 0.92 | 0.85 | 0.89 | 0.84 |
| xbrl | net_income_usd | 150 | 0.84 | 0.79 | 0.82 | 0.74 |

81 mismatches stored in `eval.mismatches`. COP and OXY revenue show up as XBRL mismatches but
match the gold set: the model reported total revenues and other income, which is the top line.

Stage 6, first build on the real warehouse (2026-11-10, dbt 1.12.6, dbt-duckdb 1.11.1, 150
filings): 1 seed, 4 views, 5 tables, 43 data tests; PASS=53 WARN=0 ERROR=0 in 0.44 s. No grain,
range or consistency test fired on the real data, including the record-vs-tasks check.

## Next step

Stage 7: an Airflow DAG (Astro) that runs ingest → parse → extract → evaluate → dbt build.
