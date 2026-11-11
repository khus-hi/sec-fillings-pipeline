"""Streamlit app queries and the app itself, on a sample warehouse built by dbt, so the SQL is
tested against the real mart schema."""

from __future__ import annotations

from datetime import datetime

import duckdb
import pytest

pytest.importorskip("pandas")
pytest.importorskip("dbt.cli.main")

from app import queries as q  # noqa: E402
from evaluation.evaluate import DDL as EVAL_DDL  # noqa: E402
from ingestion.config import REPO_ROOT, Settings  # noqa: E402
from pipeline.steps import dbt_build_step  # noqa: E402
from tests.test_dbt import MODEL, make_warehouse  # noqa: E402


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("app")
    path = tmp / "warehouse.duckdb"
    make_warehouse(path)
    dbt_build_step(Settings(None, tmp, path))  # in a subprocess: no lingering dbt handle
    con = duckdb.connect(str(path))
    con.execute(EVAL_DDL)
    now = datetime(2026, 1, 1)
    con.execute("INSERT INTO eval.runs VALUES ('r1', ?, 'v', '3', 32, 80, 0.005, ?)", [MODEL, now])
    for ref, field, f1 in [("gold", "revenue_usd", 0.92), ("xbrl", "revenue_usd", 0.89),
                           ("gold", "risk_category", 0.84),
                           ("gold", "risk_category:macroeconomic", 0.5),
                           ("gold", "risk_category:macro", 0.86)]:  # fmt: skip
        con.execute(
            "INSERT INTO eval.field_scores (run_id, model, reference, field, n, f1) "
            "VALUES ('r1', ?, ?, ?, 32, ?)",
            [MODEL, ref, field, f1],
        )
    # A later run without the gold files scores only XBRL; the gold scores of r1 still apply.
    con.execute("INSERT INTO eval.runs VALUES ('r2', ?, 'v', '3', 0, 0, 0.005, ?)",
                [MODEL, datetime(2026, 2, 1)])  # fmt: skip
    con.execute(
        "INSERT INTO eval.field_scores (run_id, model, reference, field, n, f1) "
        "VALUES ('r2', ?, 'xbrl', 'revenue_usd', 150, 0.9)",
        [MODEL],
    )
    con.close()
    return path


@pytest.fixture
def con(warehouse):
    with q.connect(warehouse) as c:
        yield c


def test_connect_is_read_only(con):
    with pytest.raises(duckdb.Error):
        con.execute("CREATE TABLE marts.x (a INT)")


def test_connect_missing_warehouse(tmp_path):
    with pytest.raises(FileNotFoundError, match="Run the pipeline first"):
        q.connect(tmp_path / "nope.duckdb")


def test_overview(con):
    ov = q.overview(con, MODEL)
    assert (ov["filings"], ov["companies"], ov["sectors"]) == (3, 2, 2)
    assert (ov["first_year"], ov["last_year"]) == (2023, 2024)
    assert ov["quarantined"] == 1 and ov["task_ok_rate"] == pytest.approx(11 / 12, abs=1e-4)


def test_risk_heatmap_all_sectors_is_reaggregated(con):
    heat = q.risk_heatmap(con, MODEL)
    y2024 = heat[heat["fiscal_year"] == 2024].set_index("category")
    assert (y2024["n_filings"] == 2).all()  # AAA (Technology) + BBB (Energy)
    assert y2024.loc["cybersecurity_technology", "share"] == pytest.approx(0.5)
    tech = q.risk_heatmap(con, MODEL, "Technology")
    assert tech[tech["fiscal_year"] == 2024].set_index("category").loc[
        "cybersecurity_technology", "share"] == 1  # fmt: skip


def test_risk_examples_filter(con):
    ex = q.risk_examples(con, MODEL, "climate_environmental")
    assert ex["ticker"].tolist() == ["BBB"]
    assert q.risk_examples(con, MODEL, "climate_environmental", "Technology").empty


def test_company_views(con):
    filings = q.company_filings(con, MODEL, "AAA")
    assert filings["fiscal_year"].tolist() == [2023, 2024]
    assert filings["revenue_growth_yoy"].iloc[1] == pytest.approx(0.2)
    assert len(q.company_risks(con, MODEL, "BBB")) == 3
    assert q.company_filings(con, MODEL, "CCC").empty  # quarantined, so no record


def test_eval_scores_latest_run_per_reference_without_per_label_rows(con):
    scores = q.eval_scores(con, MODEL)
    assert set(scores["field"]) == {"revenue_usd", "risk_category", "risk_category:macro"}
    assert len(scores) == 4
    by_ref = scores.set_index(["reference", "field"])["f1"]
    assert by_ref[("xbrl", "revenue_usd")] == 0.9  # from the newer run
    assert by_ref[("gold", "revenue_usd")] == 0.92  # kept from the run that had gold rows


def test_eval_scores_empty_before_any_evaluation(tmp_path):
    path = tmp_path / "w.duckdb"
    duckdb.connect(str(path)).close()
    with q.connect(path) as c:
        assert q.eval_scores(c, MODEL).empty


def test_quarantined_lists_the_reason(con):
    rows = q.quarantined(con, MODEL)
    assert rows["ticker"].tolist() == ["CCC"]
    assert rows["quarantine_reason"].iloc[0] == "net income larger than revenue"


def test_app_renders(warehouse, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("DUCKDB_PATH", str(warehouse))
    at = AppTest.from_file(str(REPO_ROOT / "app" / "streamlit_app.py"), default_timeout=60).run()
    assert not at.exception
    assert at.metric[0].value == "3"  # filings
    at.selectbox[0].select("Energy").run()  # sector filter on the risk tab
    at.selectbox[2].select("BBB").run()  # company tab
    assert not at.exception


def test_app_without_warehouse(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("DUCKDB_PATH", str(tmp_path / "missing.duckdb"))
    at = AppTest.from_file(str(REPO_ROOT / "app" / "streamlit_app.py"), default_timeout=60).run()
    assert not at.exception
    assert "Run the pipeline first" in at.error[0].value
