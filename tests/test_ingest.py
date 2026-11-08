"""End-to-end Stage 1 against fake EDGAR + a local store + an in-memory DuckDB."""

from pathlib import Path

import pytest

from ingestion.ingest import Company, load_companies, run
from ingestion.manifest import Manifest, sha256_hex
from ingestion.storage import LocalStore

COMPANIES = [Company("AAPL", "Information Technology"), Company("JPM", "Financials")]


@pytest.fixture
def manifest():
    m = Manifest(":memory:")
    yield m
    m.close()


def _doc_downloads(edgar) -> int:
    return sum("/Archives/" in c.request.url for c in edgar.calls)


def test_first_run_downloads_and_records_everything(edgar, client, manifest, tmp_path):
    store = LocalStore(tmp_path)
    stats = run(COMPANIES, client, store, manifest, per_company=2)

    assert (stats.listed, stats.downloaded, stats.skipped, stats.failed) == (4, 4, 0, 0)
    assert manifest.count() == 4
    assert len(store.list_keys()) == 4

    # The stored bytes hash to what the manifest says.
    key, sha, sector = manifest.con.execute(
        "SELECT raw_uri, sha256, sector FROM meta.documents WHERE ticker = 'JPM' "
        "ORDER BY filing_date DESC LIMIT 1"
    ).fetchone()
    assert sha == sha256_hex(Path(key).read_bytes())
    assert sector == "Financials"
    assert "filing_date=2025-02-14" in key


def test_rerun_is_idempotent_and_skips_downloads(edgar, client, manifest, tmp_path):
    store = LocalStore(tmp_path)
    run(COMPANIES, client, store, manifest, per_company=2)
    downloads_after_first = _doc_downloads(edgar)

    stats = run(COMPANIES, client, store, manifest, per_company=2)

    assert (stats.downloaded, stats.skipped) == (0, 4)
    assert _doc_downloads(edgar) == downloads_after_first  # no document fetched again
    assert manifest.count() == 4
    assert len(store.list_keys()) == 4
    runs = manifest.con.execute("SELECT count(*) FROM meta.ingest_runs").fetchone()[0]
    assert runs == 2


def test_asking_for_more_years_only_downloads_the_new_ones(edgar, client, manifest, tmp_path):
    store = LocalStore(tmp_path)
    run(COMPANIES, client, store, manifest, per_company=1)
    stats = run(COMPANIES, client, store, manifest, per_company=3)
    # AAPL has 2 10-Ks, JPM has 3: 5 listed, 2 already held, 3 new.
    assert (stats.listed, stats.skipped, stats.downloaded) == (5, 2, 3)
    assert manifest.count() == 5


def test_limit_caps_new_downloads(edgar, client, manifest, tmp_path):
    stats = run(COMPANIES, client, LocalStore(tmp_path), manifest, per_company=2, limit=1)
    assert stats.downloaded == 1
    assert manifest.count() == 1


def test_one_failing_document_does_not_stop_the_run(edgar, client, manifest, tmp_path):
    # Make one JPM document return 404.
    edgar.replace(
        "GET",
        "https://www.sec.gov/Archives/edgar/data/19617/000001961725000270/jpm-20241231.htm",
        status=404,
    )
    stats = run(COMPANIES, client, LocalStore(tmp_path), manifest, per_company=2)
    assert (stats.downloaded, stats.failed) == (3, 1)
    assert "0000019617-25-000270" in stats.errors[0]
    # The failed one is not in the manifest, so the next run will try it again.
    assert "0000019617-25-000270" not in manifest.known_accessions()


def test_crash_between_store_and_manifest_recovers(edgar, client, manifest, tmp_path, monkeypatch):
    """Object written, then the process dies before the manifest row. The next run
    must re-download and overwrite the same key, not create a second copy."""
    store = LocalStore(tmp_path)
    real_record = manifest.record
    calls = {"n": 0}

    def flaky_record(row):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        real_record(row)

    monkeypatch.setattr(manifest, "record", flaky_record)
    first = run(COMPANIES[:1], client, store, manifest, per_company=1)
    assert first.failed == 1 and len(store.list_keys()) == 1 and manifest.count() == 0

    second = run(COMPANIES[:1], client, store, manifest, per_company=1)
    assert second.downloaded == 1
    assert len(store.list_keys()) == 1  # same key, overwritten
    assert manifest.count() == 1


def test_unknown_ticker_in_edgar_is_reported(edgar, client, manifest, tmp_path):
    stats = run([Company("NOPE", "X")], client, LocalStore(tmp_path), manifest, per_company=1)
    assert stats.failed == 1 and "NOPE" in stats.errors[0]


def test_record_twice_updates_instead_of_duplicating(manifest):
    row = {
        "accession_number": "A-1",
        "sha256": "abc",
        "cik": "1",
        "form": "10-K",
        "filing_date": "2024-01-01",
        "source_url": "u",
        "raw_uri": "r",
        "size_bytes": 1,
        "ingested_at": "2024-01-01 00:00:00",
        "run_id": "r1",
    }
    manifest.record(row)
    manifest.record({**row, "sha256": "def", "run_id": "r2"})
    assert manifest.count() == 1
    assert manifest.con.execute("SELECT sha256 FROM meta.documents").fetchone()[0] == "def"


def test_companies_csv_has_50_unique_tickers():
    companies = load_companies()
    assert len(companies) == 50
    assert len({c.ticker for c in companies}) == 50
    assert all(c.sector for c in companies)


def test_load_companies_rejects_unknown_ticker():
    with pytest.raises(SystemExit, match="ZZZZ"):
        load_companies(tickers=["AAPL", "ZZZZ"])
