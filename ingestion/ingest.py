"""Stage 1: download 10-K filings from EDGAR into the raw zone, idempotently.

    python -m ingestion.ingest                        # all 50 companies, 3 filings each
    python -m ingestion.ingest --tickers AAPL MSFT --per-company 1   # quick smoke test

For each company: list its newest 10-Ks, skip any accession number already in the
manifest (no download at all), download the rest, hash the bytes, write them to the
raw zone, then record them in the manifest. The order matters: the object is written
*before* the manifest row, so a crash in between just means the next run downloads the
file again and overwrites the same key. Nothing is ever duplicated or half-recorded.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ingestion.config import COMPANIES_CSV, get_settings, get_store
from ingestion.edgar_client import EdgarClient, Filing
from ingestion.manifest import Manifest, sha256_hex, utcnow
from ingestion.storage import RawStore, raw_key

log = logging.getLogger("ingestion")

CONTENT_TYPES = {
    "htm": "text/html",
    "html": "text/html",
    "txt": "text/plain",
    "pdf": "application/pdf",
}


@dataclass(frozen=True)
class Company:
    ticker: str
    sector: str


@dataclass
class RunStats:
    companies: int = 0
    listed: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "errors"}


def load_companies(path: Path = COMPANIES_CSV, tickers: list[str] | None = None) -> list[Company]:
    with path.open(newline="") as fh:
        companies = [
            Company(r["ticker"].strip().upper(), r["sector"].strip()) for r in csv.DictReader(fh)
        ]
    if tickers:
        wanted = {t.upper() for t in tickers}
        unknown = wanted - {c.ticker for c in companies}
        if unknown:
            raise SystemExit(f"Not in {path.name}: {', '.join(sorted(unknown))}")
        companies = [c for c in companies if c.ticker in wanted]
    return companies


def ingest_filing(
    filing: Filing,
    company: Company,
    client: EdgarClient,
    store: RawStore,
    manifest: Manifest,
    run_id: str,
) -> int:
    """Download one filing, store it, record it. Returns bytes downloaded."""
    started = time.perf_counter()
    body = client.download(filing)
    digest = sha256_hex(body)
    ext = PurePosixPath(filing.primary_document).suffix.lstrip(".") or "htm"
    key = raw_key(filing.form, filing.filing_date, filing.cik, filing.accession_number, ext)

    if manifest.has_hash(digest):
        # Same bytes already stored under another accession number. Rare, but worth
        # knowing: later stages key on the hash, so they will process it only once.
        log.warning("duplicate content %s for %s", digest[:12], filing.accession_number)

    store.put(key, body, CONTENT_TYPES.get(ext.lower(), "application/octet-stream"))
    manifest.record(
        {
            "accession_number": filing.accession_number,
            "sha256": digest,
            "cik": filing.cik,
            "ticker": company.ticker,
            "company_name": filing.company_name,
            "sector": company.sector,
            "sic": filing.sic,
            "sic_description": filing.sic_description,
            "form": filing.form,
            "filing_date": filing.filing_date,
            "report_date": filing.report_date,
            "source_url": filing.url,
            "raw_uri": store.uri(key),
            "size_bytes": len(body),
            "ingested_at": utcnow(),
            "run_id": run_id,
        }
    )
    # One structured log line per document, easy to grep or load later.
    log.info(
        json.dumps(
            {
                "event": "stored",
                "ticker": company.ticker,
                "accession": filing.accession_number,
                "filing_date": filing.filing_date.isoformat(),
                "bytes": len(body),
                "sha256": digest[:12],
                "seconds": round(time.perf_counter() - started, 2),
                "uri": store.uri(key),
            }
        )
    )
    return len(body)


def run(
    companies: list[Company],
    client: EdgarClient,
    store: RawStore,
    manifest: Manifest,
    per_company: int,
    limit: int | None = None,
) -> RunStats:
    run_id = uuid.uuid4().hex[:12]
    stats = RunStats(companies=len(companies))
    manifest.start_run(run_id)
    started = time.perf_counter()
    known = manifest.known_accessions()
    ticker_map = client.ticker_to_cik()

    for company in companies:
        cik = ticker_map.get(company.ticker)
        if cik is None:
            stats.failed += 1
            stats.errors.append(f"{company.ticker}: ticker not found in EDGAR")
            log.error("ticker %s not found in EDGAR's ticker list", company.ticker)
            continue
        try:
            filings = client.list_filings(cik, form="10-K", limit=per_company)
        except Exception as err:  # one bad company must not stop the other 49
            stats.failed += 1
            stats.errors.append(f"{company.ticker}: listing failed: {err}")
            log.exception("could not list filings for %s", company.ticker)
            continue
        stats.listed += len(filings)

        for filing in filings:
            if filing.accession_number in known:
                stats.skipped += 1
                log.debug("skip %s %s (already ingested)", company.ticker, filing.accession_number)
                continue
            if limit is not None and stats.downloaded >= limit:
                continue
            try:
                stats.bytes_downloaded += ingest_filing(
                    filing, company, client, store, manifest, run_id
                )
                stats.downloaded += 1
                known.add(filing.accession_number)
            except Exception as err:
                stats.failed += 1
                stats.errors.append(f"{company.ticker} {filing.accession_number}: {err}")
                log.exception("failed %s %s", company.ticker, filing.accession_number)

    stats.seconds = round(time.perf_counter() - started, 2)
    manifest.finish_run(run_id, stats.as_dict())
    log.info("run %s finished: %s", run_id, json.dumps(stats.as_dict()))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download 10-K filings from EDGAR into the raw zone."
    )
    parser.add_argument(
        "--tickers", nargs="+", help="only these tickers (default: all in companies.csv)"
    )
    parser.add_argument(
        "--per-company", type=int, help="newest N 10-Ks per company (default from .env)"
    )
    parser.add_argument("--limit", type=int, help="stop after downloading this many new documents")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # urllib3 logs every retry at DEBUG; keep the output readable.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)

    settings = get_settings()
    client = EdgarClient(settings.sec_user_agent, settings.max_requests_per_second)
    store = get_store(settings)
    manifest = Manifest(settings.duckdb_path)
    try:
        stats = run(
            load_companies(tickers=args.tickers),
            client,
            store,
            manifest,
            per_company=args.per_company or settings.filings_per_company,
            limit=args.limit,
        )
        total = manifest.count()
    finally:
        manifest.close()

    print(
        f"\nlisted {stats.listed} | downloaded {stats.downloaded} | skipped {stats.skipped} "
        f"| failed {stats.failed} | {stats.bytes_downloaded / 1e6:.1f} MB in {stats.seconds}s "
        f"| manifest now holds {total} documents"
    )
    for err in stats.errors:
        print(f"  ! {err}")
    # Non-zero exit when anything failed, so Airflow (Stage 7) marks the task as failed.
    return 1 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())
