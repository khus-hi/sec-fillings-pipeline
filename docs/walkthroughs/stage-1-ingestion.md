# Stage 1 walkthrough: ingestion

## What this stage builds and why

Stage 1 downloads the newest three 10-K filings for 50 companies from SEC EDGAR and stores the
raw HTML in a local S3 bucket (SeaweedFS, running in Docker), in folders named by filing date. Every document is
recorded in a manifest table with its SHA-256 hash, so running the job again downloads nothing it
already has. This matters because everything downstream (parsing, LLM calls, evaluation) is slow or
costly, and a pipeline you can re-run safely is one you can schedule, debug and trust. The raw zone
is also your "source of truth": if a later stage has a bug, you fix it and re-run from raw without
going back to SEC.

## File by file

### `ingestion/config.py`
Reads every setting from `.env` into a frozen dataclass. Same pattern as Project 1.
- `load_dotenv(REPO_ROOT / ".env")` loads the file from the repo root no matter where you run from.
- `get_store()` returns `S3Store` or `LocalStore` depending on `STORAGE_BACKEND`. The rest of the
  code never knows which one it has. **Why:** you can test against a folder and run against SeaweedFS
  or AWS with no code change.

### `ingestion/edgar_client.py`
Talks to three free SEC endpoints.
- `EdgarClient.__init__` refuses to start without an email in `SEC_USER_AGENT`. SEC blocks
  anonymous scrapers, so failing early with a clear message beats a confusing 403 later.
- `Retry(total=4, backoff_factor=1.0, status_forcelist=(429, 500, ...))`: transient failures are
  retried after 1, 2, 4, 8 seconds. 404 is not retried, because a missing file stays missing.
- `RateLimiter.wait()`: sleeps so calls are at least 0.2 s apart (5 per second). **Why not just
  `time.sleep(0.2)` everywhere?** The limiter only sleeps the remaining time, and it is
  thread-safe, so it still works if we parallelise downloads later.
- `list_filings()`: EDGAR returns filings *column-wise* (one list of forms, one list of dates...).
  `_filings_from_block` zips them into rows and keeps only `form == "10-K"`. The tricky part:
  the JSON holds only the newest ~1,000 filings. A bank like JPMorgan files hundreds of
  prospectuses a year, which can push its 10-Ks out of that window, so we fetch the older
  "history pages" only when we still need more filings. That's a real-world edge case you can
  talk about.
- `Filing.url` builds the document URL from the CIK, the accession number without dashes, and the
  primary document name.

### `ingestion/storage.py`
- `raw_key()` builds `raw/edgar/10-K/filing_date=2024-11-01/<cik>_<accession>.htm`.
  `filing_date=` is Hive-style partitioning, which DuckDB, Spark and Athena understand natively
  (they can filter on the folder name without opening files).
- **Why filing date and not today's date?** The key must be the same every time we see the same
  filing. If we partitioned by download date, running on Monday and again on Tuesday would store
  two copies.
- `LocalStore.put` writes to a `.tmp` file then renames it: a crash never leaves a half file under
  the real name. `S3Store.put` doesn't need that, because S3 uploads are already all-or-nothing.

### `ingestion/manifest.py`
Two DuckDB tables:
- `meta.documents`: one row per filing. Primary key is `accession_number` (EDGAR's unique id).
  Also stores `sha256`, company, sector, SIC industry code, dates and where the file lives.
- `meta.ingest_runs`: one row per run with counts and duration. Your résumé throughput numbers
  will come from here, not from guesses.
- `record()` uses `INSERT ... ON CONFLICT (accession_number) DO UPDATE`. That is an *upsert*:
  calling it twice for the same filing updates the row instead of adding a second one.

### `ingestion/ingest.py`
The job itself. For each company: look up its CIK, list its 10-Ks, then for each filing:
1. If the accession number is already in the manifest → skip (no download).
2. Otherwise download, hash, `store.put`, then `manifest.record`.

Key choices:
- **Object first, manifest second.** If the process dies between the two, the next run sees no
  manifest row and downloads again to the same key (overwrite, no duplicate). The opposite order
  could leave a manifest row pointing at a file that doesn't exist. There's a test that simulates
  exactly this crash.
- **One bad company or document doesn't stop the run** (`try/except` per item), but the CLI exits
  with code 1 if anything failed, so Airflow will mark the task red in Stage 7.
- One JSON log line per stored document: easy to grep now, easy to load into a table later.

### `ingestion/companies.csv`
50 tickers across 11 sectors (5 each, except Materials 3 and Real Estate 2). The sector column feeds
the Stage 6 mart (risk categories by industry). EDGAR's SIC code is stored too, as a second, official
industry label. The optional `ciks` column pins one or more CIKs for a ticker. It exists because of a
real failure: ExxonMobil's ticker moved to a new holding company with no 10-K history yet, so the
first full run silently skipped it. Now a company with 0 filings fails the run, and XOM lists both
the new and the old CIK.

### `docker-compose.yml`
Runs SeaweedFS, an open-source S3-compatible server. boto3 talks to it exactly like AWS because
`.env` sets `S3_ENDPOINT_URL=http://localhost:8333`. `S3Store.from_settings` creates the `sec-raw`
bucket on first use when an endpoint URL is set, so there is no manual setup step.

**Why not MinIO?** That was the first choice, but MinIO stopped publishing free Docker images and
`docker pull minio/minio` now fails. Because all storage code sits behind one small interface,
swapping the server was a docker-compose change plus one `.env` line, with no pipeline code
changed. That is a good interview story about why the abstraction exists.

### `tests/`
30 tests, all offline. `responses` fakes EDGAR's HTTP answers and `moto` fakes S3, so CI needs no
network, email or keys. The storage tests run against **both** backends with one parametrized
fixture, which proves they behave the same. The ingest tests cover: first run, re-run (zero
downloads), asking for more years (only the new ones download), a 404 on one document, and the
crash between object and manifest.

## Alternatives considered

| Instead of | We could have | Why not |
|---|---|---|
| EDGAR JSON APIs | `sec-edgar-downloader` / `edgartools` libraries | hides the HTTP, rate limiting and idempotency you need to explain in an interview |
| Manifest in DuckDB | a `manifest.json` in the bucket | no SQL, and two runs could overwrite each other's file |
| Skip by accession | download then compare hashes | every re-run would re-download ~500 MB from SEC |
| Sequential downloads | a thread pool | SEC's rate limit is the bottleneck anyway; simpler to reason about |
