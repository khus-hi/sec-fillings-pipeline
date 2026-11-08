"""Stage 2 end to end: manifest + raw store in, parsed tables out, idempotently."""

from datetime import date

import pytest

from ingestion.manifest import Manifest, sha256_hex, utcnow
from ingestion.storage import LocalStore, raw_key
from parsing import parse
from tests.sample_10k import build_10k


def _ingest(manifest: Manifest, store: LocalStore, ticker: str, accession: str, body: bytes):
    key = raw_key("10-K", date(2024, 11, 1), "0000000001", accession, "htm")
    store.put(key, body, "text/html")
    manifest.record(
        {
            "accession_number": accession,
            "sha256": sha256_hex(body),
            "cik": "0000000001",
            "ticker": ticker,
            "form": "10-K",
            "filing_date": date(2024, 11, 1),
            "source_url": "u",
            "raw_uri": store.uri(key),
            "size_bytes": len(body),
            "ingested_at": utcnow(),
            "run_id": "test",
        }
    )


@pytest.fixture
def env(tmp_path):
    manifest = Manifest(":memory:")
    store = LocalStore(tmp_path)
    _ingest(manifest, store, "AAA", "A-1", build_10k())
    _ingest(manifest, store, "BBB", "B-1", build_10k(n_risks=40))
    yield manifest.con, store
    manifest.close()


def _count(con, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_parse_stores_sections_and_chunks(env):
    con, store = env
    stats = parse.run(con, store)
    assert (stats["parsed"], stats["skipped"], stats["failed"]) == (2, 0, 0)
    assert _count(con, "parsed.sections") == 8  # 4 sections x 2 docs
    status, found, missing = con.execute(
        "SELECT status, sections_found, sections_missing FROM parsed.documents "
        "WHERE accession_number = 'A-1'"
    ).fetchone()
    assert status == "ok"
    assert found == ["item_1", "item_1a", "item_7", "item_8"]
    assert missing == []
    n_chunks = con.execute("SELECT sum(n_chunks) FROM parsed.documents").fetchone()[0]
    assert n_chunks == _count(con, "parsed.chunks") > 0


def test_rerun_skips_and_does_not_duplicate(env):
    con, store = env
    parse.run(con, store)
    before = (_count(con, "parsed.sections"), _count(con, "parsed.chunks"))
    stats = parse.run(con, store)
    assert (stats["parsed"], stats["skipped"]) == (0, 2)
    assert (_count(con, "parsed.sections"), _count(con, "parsed.chunks")) == before


def test_force_reparses_without_duplicates(env):
    con, store = env
    parse.run(con, store)
    before = _count(con, "parsed.chunks")
    stats = parse.run(con, store, force=True)
    assert stats["parsed"] == 2
    assert _count(con, "parsed.chunks") == before
    assert _count(con, "parsed.documents") == 2


def test_new_parser_version_reparses(env, monkeypatch):
    con, store = env
    parse.run(con, store)
    monkeypatch.setattr(parse, "PARSER_VERSION", "next")
    stats = parse.run(con, store)
    assert stats["parsed"] == 2
    versions = con.execute("SELECT DISTINCT parser_version FROM parsed.documents").fetchall()
    assert versions == [("next",)]


def test_tickers_filter_and_limit(env):
    con, store = env
    assert parse.run(con, store, tickers=["bbb"])["parsed"] == 1
    assert parse.run(con, store, limit=0)["parsed"] == 0


def test_identical_bytes_parsed_once(env):
    con, store = env
    manifest_con = con
    # Same document under a second accession number (e.g. a re-filed copy).
    from ingestion.manifest import Manifest as _M  # noqa: F401  (schema already exists)

    body = build_10k()
    key = raw_key("10-K", date(2024, 11, 2), "0000000001", "A-2", "htm")
    store.put(key, body, "text/html")
    manifest_con.execute(
        "INSERT INTO meta.documents SELECT 'A-2', sha256, cik, ticker, company_name, sector, sic, "
        "sic_description, form, filing_date, report_date, source_url, ?, size_bytes, "
        "ingested_at, run_id FROM meta.documents WHERE accession_number = 'A-1'",
        [store.uri(key)],
    )
    stats = parse.run(con, store)
    assert stats["parsed"] == 2  # 3 accessions, 2 distinct hashes


def test_bad_document_fails_alone_and_is_retried(env, monkeypatch):
    con, store = env
    real = parse.parse_document

    def flaky(raw):
        if b"n_risks" not in raw and raw.count(b"supply chains") == 40:
            raise RuntimeError("boom")
        return real(raw)

    monkeypatch.setattr(parse, "parse_document", flaky)
    stats = parse.run(con, store)
    assert (stats["parsed"], stats["failed"]) == (1, 1)
    status, error = con.execute(
        "SELECT status, error FROM parsed.documents WHERE accession_number = 'B-1'"
    ).fetchone()
    assert status == "failed" and "boom" in error

    monkeypatch.setattr(parse, "parse_document", real)
    stats = parse.run(con, store)
    assert (stats["parsed"], stats["skipped"]) == (1, 1)  # only the failed one again


def test_crash_mid_save_leaves_no_partial_rows(env, monkeypatch):
    con, store = env
    parse.run(con, store, tickers=["AAA"])
    before = _count(con, "parsed.chunks")

    real_save = parse.save_document

    def crash_after_delete(con_, sha, acc, result, seconds):
        result = {**result, "chunks": {"item_1": [None]}}  # makes the chunk insert blow up
        return real_save(con_, sha, acc, result, seconds)

    monkeypatch.setattr(parse, "save_document", crash_after_delete)
    stats = parse.run(con, store, tickers=["AAA"], force=True)
    assert stats["failed"] == 1
    # The rollback restored the old sections and chunks.
    assert _count(con, "parsed.chunks") == before
    assert _count(con, "parsed.sections") == 4


def test_found_by_is_stored_and_old_schema_is_upgraded(env):
    con, store = env
    # A database created by parser version 1 has no found_by column.
    con.execute("CREATE SCHEMA IF NOT EXISTS parsed")
    con.execute(
        "CREATE TABLE parsed.sections (sha256 VARCHAR NOT NULL, section VARCHAR NOT NULL, "
        "start_offset BIGINT NOT NULL, char_count BIGINT NOT NULL, is_short BOOLEAN NOT NULL, "
        "text VARCHAR NOT NULL, PRIMARY KEY (sha256, section))"
    )
    parse.run(con, store)
    rows = con.execute("SELECT DISTINCT found_by FROM parsed.sections").fetchall()
    assert rows == [("heading",)]
