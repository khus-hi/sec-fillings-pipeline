"""The ingestion manifest: one row per filing we have stored, in DuckDB.

It is the pipeline's memory. Before downloading, ingestion asks "do I already have
this accession number?" and skips it if so. Each row also stores the SHA-256 of the
raw bytes, which later stages use as the document's identity: a parse or LLM result
is keyed by content hash, so unchanged documents are never re-processed.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import duckdb

DDL = """
CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS meta.documents (
    accession_number VARCHAR PRIMARY KEY,   -- EDGAR's unique id for a filing
    sha256           VARCHAR NOT NULL,      -- hash of the raw bytes we stored
    cik              VARCHAR NOT NULL,
    ticker           VARCHAR,
    company_name     VARCHAR,
    sector           VARCHAR,               -- from ingestion/companies.csv
    sic              VARCHAR,               -- SEC industry code
    sic_description  VARCHAR,
    form             VARCHAR NOT NULL,
    filing_date      DATE NOT NULL,
    report_date      DATE,
    source_url       VARCHAR NOT NULL,
    raw_uri          VARCHAR NOT NULL,      -- where the bytes live (s3://... or a path)
    size_bytes       BIGINT NOT NULL,
    ingested_at      TIMESTAMP NOT NULL,
    run_id           VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS meta.ingest_runs (
    run_id       VARCHAR PRIMARY KEY,
    started_at   TIMESTAMP NOT NULL,
    finished_at  TIMESTAMP,
    companies    INTEGER,
    listed       INTEGER,   -- filings found in EDGAR
    downloaded   INTEGER,   -- new documents stored this run
    skipped      INTEGER,   -- already in the manifest, no download
    failed       INTEGER,
    bytes_downloaded BIGINT,
    seconds      DOUBLE
);
"""


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def utcnow() -> datetime:
    # DuckDB TIMESTAMP has no time zone, so store naive UTC.
    return datetime.now(UTC).replace(tzinfo=None)


class Manifest:
    def __init__(self, db_path: Path | str) -> None:
        if isinstance(db_path, Path):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(db_path))
        self.con.execute(DDL)

    def close(self) -> None:
        self.con.close()

    def known_accessions(self) -> set[str]:
        rows = self.con.execute("SELECT accession_number FROM meta.documents").fetchall()
        return {r[0] for r in rows}

    def has_hash(self, sha256: str) -> bool:
        row = self.con.execute(
            "SELECT 1 FROM meta.documents WHERE sha256 = ? LIMIT 1", [sha256]
        ).fetchone()
        return row is not None

    def record(self, row: dict) -> None:
        """Insert one document. ON CONFLICT makes this safe to call twice for the
        same accession (e.g. a retry after a crash): the row is updated, not duplicated."""
        cols = list(row)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "accession_number")
        self.con.execute(
            f"INSERT INTO meta.documents ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT (accession_number) DO UPDATE SET {updates}",
            list(row.values()),
        )

    def start_run(self, run_id: str) -> None:
        self.con.execute(
            "INSERT INTO meta.ingest_runs (run_id, started_at) VALUES (?, ?)", [run_id, utcnow()]
        )

    def finish_run(self, run_id: str, stats: dict) -> None:
        self.con.execute(
            """UPDATE meta.ingest_runs SET finished_at = ?, companies = ?, listed = ?,
               downloaded = ?, skipped = ?, failed = ?, bytes_downloaded = ?, seconds = ?
               WHERE run_id = ?""",
            [
                utcnow(),
                stats["companies"],
                stats["listed"],
                stats["downloaded"],
                stats["skipped"],
                stats["failed"],
                stats["bytes_downloaded"],
                stats["seconds"],
                run_id,
            ],
        )

    def count(self) -> int:
        return self.con.execute("SELECT count(*) FROM meta.documents").fetchone()[0]
