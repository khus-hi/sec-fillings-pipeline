"""dbt models (Stage 6), built against a small sample warehouse.

The sample is written with the same DDL the Python stages use, so a column renamed in Python
breaks this test before it breaks the real warehouse. Runs fully offline: dbt-duckdb only.
"""

from __future__ import annotations

import csv
import os
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from extraction.extract import ensure_schema as extraction_schema
from extraction.schema import CATEGORY_DESCRIPTIONS
from ingestion.config import REPO_ROOT
from ingestion.manifest import Manifest

dbt_main = pytest.importorskip("dbt.cli.main")

DBT_DIR = REPO_ROOT / "dbt"
MODEL = "qwen3:8b"
NOW = datetime(2026, 1, 1)

# (ticker, sector, fiscal_year, revenue, net income, employees, risk categories)
RECORDS = [
    ("AAA", "Technology", 2023, 100_000_000_000, 10_000_000_000, 1000,
     ["cybersecurity_technology", "market_competition"]),
    ("AAA", "Technology", 2024, 120_000_000_000, 15_000_000_000, 1200,
     ["cybersecurity_technology"]),
    ("BBB", "Energy", 2024, 50_000_000_000, -5_000_000_000, None,
     ["climate_environmental", "macroeconomic", "geopolitical"]),
]  # fmt: skip
QUARANTINED = ("CCC", "Energy", 2024)  # financials failed validation three times


def _document(con, ticker, sector, year, n):
    sha = f"{ticker}{year}-{n}".ljust(64, "0")
    accession = f"0000000000-{year % 100:02d}-{n:06d}"
    con.execute(
        "INSERT INTO meta.documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [accession, sha, f"{n:010d}", ticker, f"{ticker} Corp", sector, "1000", "Industry",
         "10-K", date(year + 1, 2, 15), date(year, 12, 31), "https://example.com",
         f"s3://raw/{accession}.htm", 1000, NOW, "run1"],
    )  # fmt: skip
    return sha, accession


def _task(con, sha, task, status="ok", attempts=1):
    con.execute(
        "INSERT INTO extracted.task_results VALUES "
        "(?, ?, ?, 's1-p1', ?, ?, '{}', [], 1000, 2.0, 500, 50, 0.0, ?)",
        [sha, task, MODEL, status, attempts, NOW],
    )
    if status == "quarantined":
        con.execute(
            "INSERT INTO extracted.quarantine VALUES (?, ?, ?, 's1-p1', ?, ?, NULL, ?)",
            [sha, task, MODEL, attempts, "net income larger than revenue", NOW],
        )


def make_warehouse(path: Path, extra_records: list | None = None) -> None:
    Manifest(path).close()  # meta.documents
    con = duckdb.connect(str(path))
    extraction_schema(con)
    for n, (ticker, sector, year, rev, ni, emp, cats) in enumerate(
        RECORDS + (extra_records or []), start=1
    ):
        sha, accession = _document(con, ticker, sector, year, n)
        for task in ("financials", "employees", "risk_factors"):
            _task(con, sha, task, attempts=2 if (ticker, task) == ("BBB", "financials") else 1)
        con.execute(
            "INSERT INTO extracted.filings VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', 'v', ?)",
            [sha, MODEL, accession, f"{n:010d}", ticker, f"{ticker} Corp", date(year, 12, 31),
             year, rev, ni, emp, [{"title": f"Risk {c}", "category": c} for c in cats],
             ["low employee count"] if emp and emp < 5000 else [], NOW],
        )  # fmt: skip
    sha, _ = _document(con, *QUARANTINED, 99)
    _task(con, sha, "financials", status="quarantined", attempts=3)
    _task(con, sha, "employees")
    _task(con, sha, "risk_factors")
    con.close()


def dbt_build(tmp_path: Path, warehouse: Path):
    os.environ["DBT_DUCKDB_PATH"] = str(warehouse)
    try:
        return dbt_main.dbtRunner().invoke([
            "build", "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR),
            "--target-path", str(tmp_path / "target"), "--log-path", str(tmp_path / "logs"),
            "--quiet",
        ])  # fmt: skip
    finally:
        del os.environ["DBT_DUCKDB_PATH"]


def failed_nodes(result) -> list[str]:
    return [r.node.name for r in result.result or [] if str(r.status) in ("error", "fail")]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dbt")
    warehouse = tmp / "warehouse.duckdb"
    make_warehouse(warehouse)
    result = dbt_build(tmp, warehouse)
    assert result.success, failed_nodes(result) or result.exception
    con = duckdb.connect(str(warehouse))  # dbt-duckdb keeps its own handle open in-process
    yield con
    con.close()


def rows(con, sql):
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def test_fct_filings_ratios_and_growth(built):
    by_key = {
        (r["ticker"], r["fiscal_year"]): r for r in rows(built, "SELECT * FROM marts.fct_filings")
    }
    assert set(by_key) == {("AAA", 2023), ("AAA", 2024), ("BBB", 2024)}  # CCC was quarantined
    aaa = by_key[("AAA", 2024)]
    assert aaa["revenue_growth_yoy"] == pytest.approx(0.2)
    assert aaa["employee_change_yoy"] == 200
    assert aaa["net_margin"] == pytest.approx(0.125)
    assert aaa["revenue_per_employee_usd"] == pytest.approx(100_000_000)
    assert aaa["sector"] == "Technology" and aaa["is_flagged"]
    assert by_key[("AAA", 2023)]["revenue_growth_yoy"] is None  # no prior year
    bbb = by_key[("BBB", 2024)]
    assert bbb["net_margin"] == pytest.approx(-0.1)
    assert bbb["revenue_per_employee_usd"] is None  # employee count not found


def test_risk_trends_cover_every_category(built):
    trends = rows(built, "SELECT * FROM marts.mart_risk_trends")
    groups = {(r["sector"], r["fiscal_year"]) for r in trends}
    assert groups == {("Technology", 2023), ("Technology", 2024), ("Energy", 2024)}
    assert len(trends) == 3 * len(CATEGORY_DESCRIPTIONS)
    share = {(r["sector"], r["fiscal_year"], r["category"]): r["share_of_filings"] for r in trends}
    assert share[("Technology", 2024, "cybersecurity_technology")] == 1
    assert share[("Technology", 2024, "market_competition")] == 0
    assert share[("Energy", 2024, "climate_environmental")] == 1
    assert share[("Energy", 2024, "human_capital")] == 0


def test_risk_factors_keep_rank_and_label(built):
    risks = rows(
        built, "SELECT * FROM marts.fct_risk_factors WHERE ticker = 'BBB' ORDER BY risk_rank"
    )
    assert [r["risk_rank"] for r in risks] == [1, 2, 3]
    assert risks[0]["category_label"] == "Climate & environment"


def test_model_summary_counts_quarantine_and_retries(built):
    (summary,) = rows(built, "SELECT * FROM marts.mart_model_summary")
    assert summary["model"] == MODEL
    assert summary["n_documents"] == 4
    assert (summary["n_tasks_ok"], summary["n_tasks_quarantined"]) == (11, 1)
    assert summary["n_retries"] == 1 + 2  # BBB financials once, CCC financials twice
    assert summary["n_records"] == 3
    assert summary["employee_coverage"] == pytest.approx(2 / 3, abs=1e-4)


def test_dim_companies(built):
    companies = {r["ticker"]: r for r in rows(built, "SELECT * FROM marts.dim_companies")}
    assert set(companies) == {"AAA", "BBB", "CCC"}
    assert (companies["AAA"]["n_filings"], companies["AAA"]["last_fiscal_year"]) == (2, 2024)


def test_duplicate_company_year_fails_the_build(tmp_path):
    """Two records for the same company and fiscal year (e.g. an amendment slipping in) must
    stop the build, not double-count the company in every mart."""
    warehouse = tmp_path / "warehouse.duckdb"
    make_warehouse(warehouse, extra_records=[
        ("AAA", "Technology", 2024, 121_000_000_000, 15_000_000_000, 1200, ["macroeconomic"]),
    ])  # fmt: skip
    result = dbt_build(tmp_path, warehouse)
    assert not result.success
    assert any("unique_combination_fct_filings" in name for name in failed_nodes(result))


def test_category_seed_matches_the_schema():
    """The dbt seed and the extraction schema must list the same categories and definitions."""
    with (DBT_DIR / "seeds" / "risk_categories.csv").open(newline="") as f:
        seed = {r["category"]: r["definition"] for r in csv.DictReader(f)}
    assert seed == {str(getattr(k, "value", k)): v for k, v in CATEGORY_DESCRIPTIONS.items()}
