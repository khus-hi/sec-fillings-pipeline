"""Stage 2: raw 10-K HTML -> clean text -> sections -> chunks, stored in DuckDB.

    python -m parsing.parse                 # parse every ingested filing not yet parsed
    python -m parsing.parse --tickers AAPL  # just one company
    python -m parsing.parse --force         # re-parse everything

Idempotency: a document is identified by the SHA-256 of its raw bytes (from the
Stage 1 manifest). A document is skipped when `parsed.documents` already holds that
hash with status "ok" and the current PARSER_VERSION. Changing the parsing logic means
bumping PARSER_VERSION, and the next run re-parses everything under the new version.
Each document's rows are replaced inside one transaction, so a crash mid-document
leaves the previous result intact rather than half a document.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid

import duckdb

from ingestion.config import get_settings, get_store
from ingestion.manifest import Manifest, utcnow
from ingestion.storage import RawStore
from parsing.chunking import chunk_text
from parsing.html_text import html_to_text
from parsing.sections import WANTED, extract_sections

log = logging.getLogger("parsing")

PARSER_VERSION = "2"  # 2: title fallback for short Item 1A / 7 / 8

DDL = """
CREATE SCHEMA IF NOT EXISTS parsed;

CREATE TABLE IF NOT EXISTS parsed.documents (
    sha256           VARCHAR PRIMARY KEY,
    accession_number VARCHAR NOT NULL,
    parser_version   VARCHAR NOT NULL,
    status           VARCHAR NOT NULL,      -- ok | failed
    error            VARCHAR,
    text_chars       BIGINT,
    sections_found   VARCHAR[],
    sections_missing VARCHAR[],
    sections_short   VARCHAR[],             -- found but suspiciously small
    n_chunks         INTEGER,
    seconds          DOUBLE,
    parsed_at        TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS parsed.sections (
    sha256       VARCHAR NOT NULL,
    section      VARCHAR NOT NULL,          -- item_1 | item_1a | item_7 | item_8
    start_offset BIGINT NOT NULL,
    char_count   BIGINT NOT NULL,
    is_short     BOOLEAN NOT NULL,
    text         VARCHAR NOT NULL,
    PRIMARY KEY (sha256, section)
);
-- Added in parser version 2: which rule located the section (heading | title).
ALTER TABLE parsed.sections ADD COLUMN IF NOT EXISTS found_by VARCHAR;

CREATE TABLE IF NOT EXISTS parsed.chunks (
    chunk_id      VARCHAR PRIMARY KEY,      -- <sha256[:16]>:<section>:<index>
    sha256        VARCHAR NOT NULL,
    section       VARCHAR NOT NULL,
    chunk_index   INTEGER NOT NULL,
    char_count    INTEGER NOT NULL,
    approx_tokens INTEGER NOT NULL,
    text          VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS meta.parse_runs (
    run_id         VARCHAR PRIMARY KEY,
    parser_version VARCHAR NOT NULL,
    started_at     TIMESTAMP NOT NULL,
    finished_at    TIMESTAMP,
    parsed         INTEGER,
    skipped        INTEGER,
    failed         INTEGER,
    seconds        DOUBLE
);
"""


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(DDL)


def pending_documents(
    con: duckdb.DuckDBPyConnection, tickers: list[str] | None, force: bool
) -> tuple[list[tuple[str, str, str, str]], int]:
    """(sha256, accession, ticker, raw_uri) for documents needing a parse, plus the
    number skipped. One row per hash: identical bytes are parsed once."""
    where, params = "", []
    if tickers:
        where = f"WHERE d.ticker IN ({', '.join('?' for _ in tickers)})"
        params = [t.upper() for t in tickers]
    rows = con.execute(
        f"""
        SELECT d.sha256, min(d.accession_number), min(d.ticker), min(d.raw_uri),
               bool_or(p.status = 'ok' AND p.parser_version = ?) AS done
        FROM meta.documents d
        LEFT JOIN parsed.documents p USING (sha256)
        {where}
        GROUP BY d.sha256
        ORDER BY min(d.ticker), min(d.filing_date) DESC
        """,
        [PARSER_VERSION, *params],
    ).fetchall()
    todo = [r[:4] for r in rows if force or not r[4]]
    return todo, len(rows) - len(todo)


def parse_document(raw: bytes) -> dict:
    """Pure function: raw bytes in, everything we store out. Easy to unit test."""
    text = html_to_text(raw)
    sections = extract_sections(text)
    chunks = {name: chunk_text(sec.text) for name, sec in sections.items()}
    return {"text_chars": len(text), "sections": sections, "chunks": chunks}


def save_document(
    con: duckdb.DuckDBPyConnection, sha256: str, accession: str, result: dict, seconds: float
) -> None:
    sections = result["sections"]
    con.execute("BEGIN TRANSACTION")
    try:
        for table in ("parsed.chunks", "parsed.sections", "parsed.documents"):
            con.execute(f"DELETE FROM {table} WHERE sha256 = ?", [sha256])
        for name, sec in sections.items():
            con.execute(
                "INSERT INTO parsed.sections (sha256, section, start_offset, char_count, "
                "is_short, text, found_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [sha256, name, sec.start, len(sec.text), sec.is_short, sec.text, sec.found_by],
            )
        chunk_rows = [
            [f"{sha256[:16]}:{name}:{c.index:03d}", sha256, name, c.index, len(c.text),
             c.approx_tokens, c.text]
            for name, chunks in result["chunks"].items()
            for c in chunks
        ]  # fmt: skip
        if chunk_rows:
            con.executemany("INSERT INTO parsed.chunks VALUES (?, ?, ?, ?, ?, ?, ?)", chunk_rows)
        found = sorted(sections)
        con.execute(
            "INSERT INTO parsed.documents VALUES (?, ?, ?, 'ok', NULL, ?, ?, ?, ?, ?, ?, ?)",
            [
                sha256,
                accession,
                PARSER_VERSION,
                result["text_chars"],
                found,
                [f"item_{i}" for i in WANTED if f"item_{i}" not in sections],
                [n for n in found if sections[n].is_short],
                len(chunk_rows),
                seconds,
                utcnow(),
            ],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def record_failure(con, sha256: str, accession: str, error: str, seconds: float) -> None:
    con.execute("DELETE FROM parsed.documents WHERE sha256 = ?", [sha256])
    con.execute(
        "INSERT INTO parsed.documents (sha256, accession_number, parser_version, status, error, "
        "seconds, parsed_at) VALUES (?, ?, ?, 'failed', ?, ?, ?)",
        [sha256, accession, PARSER_VERSION, error[:1000], seconds, utcnow()],
    )


def run(
    con: duckdb.DuckDBPyConnection,
    store: RawStore,
    tickers: list[str] | None = None,
    force: bool = False,
    limit: int | None = None,
) -> dict:
    ensure_schema(con)
    run_id = uuid.uuid4().hex[:12]
    con.execute(
        "INSERT INTO meta.parse_runs (run_id, parser_version, started_at) VALUES (?, ?, ?)",
        [run_id, PARSER_VERSION, utcnow()],
    )
    todo, skipped = pending_documents(con, tickers, force)
    if limit is not None:
        todo = todo[:limit]
    stats = {"parsed": 0, "skipped": skipped, "failed": 0, "errors": []}
    started = time.perf_counter()

    for sha256, accession, ticker, raw_uri in todo:
        t0 = time.perf_counter()
        try:
            raw = store.get(store.key_from_uri(raw_uri))
            result = parse_document(raw)
            seconds = round(time.perf_counter() - t0, 3)
            save_document(con, sha256, accession, result, seconds)
            stats["parsed"] += 1
            secs = result["sections"]
            log.info(
                json.dumps(
                    {
                        "event": "parsed",
                        "ticker": ticker,
                        "accession": accession,
                        "seconds": seconds,
                        "text_chars": result["text_chars"],
                        "sections": {n: len(s.text) for n, s in sorted(secs.items())},
                        "missing": [f"item_{i}" for i in WANTED if f"item_{i}" not in secs],
                        "short": sorted(n for n, s in secs.items() if s.is_short),
                        "by_title": sorted(n for n, s in secs.items() if s.found_by == "title"),
                        "chunks": sum(len(c) for c in result["chunks"].values()),
                    }
                )
            )
        except Exception as err:  # one bad document must not stop the run
            seconds = round(time.perf_counter() - t0, 3)
            record_failure(con, sha256, accession, f"{type(err).__name__}: {err}", seconds)
            stats["failed"] += 1
            stats["errors"].append(f"{ticker} {accession}: {type(err).__name__}: {err}")
            log.exception("failed to parse %s %s", ticker, accession)

    stats["seconds"] = round(time.perf_counter() - started, 2)
    con.execute(
        "UPDATE meta.parse_runs SET finished_at = ?, parsed = ?, skipped = ?, failed = ?, "
        "seconds = ? WHERE run_id = ?",
        [utcnow(), stats["parsed"], stats["skipped"], stats["failed"], stats["seconds"], run_id],
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse ingested 10-Ks into sections and chunks.")
    parser.add_argument("--tickers", nargs="+", help="only these tickers")
    parser.add_argument("--force", action="store_true", help="re-parse already parsed documents")
    parser.add_argument("--limit", type=int, help="parse at most this many documents")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)

    settings = get_settings()
    manifest = Manifest(settings.duckdb_path)  # also guarantees meta.documents exists
    try:
        stats = run(manifest.con, get_store(settings), args.tickers, args.force, args.limit)
    finally:
        manifest.close()

    rate = stats["parsed"] / stats["seconds"] if stats["seconds"] else 0
    print(
        f"\nparsed {stats['parsed']} | skipped {stats['skipped']} | failed {stats['failed']} "
        f"| {stats['seconds']}s ({rate:.2f} docs/s)"
    )
    for err in stats["errors"]:
        print(f"  ! {err}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
