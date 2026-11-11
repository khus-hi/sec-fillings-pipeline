"""SQL behind the Streamlit app. Plain functions over a DuckDB connection, so they are tested
without Streamlit and app/streamlit_app.py only does layout.

Everything reads the dbt marts and the evaluation tables. Nothing here writes.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Read-only, so the app can never change the warehouse. DuckDB allows one writing
    process, so the app opens a short-lived connection per query instead of holding one."""
    if not Path(db_path).exists():
        raise FileNotFoundError(f"No warehouse at {db_path}. Run the pipeline first.")
    return duckdb.connect(str(db_path), read_only=True)


def _has_table(con, schema: str, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
        [schema, table]).fetchone()[0])  # fmt: skip


def models(con) -> list[str]:
    """Models with records, the one with the most records first (the default view)."""
    rows = con.execute(
        "SELECT model FROM marts.fct_filings GROUP BY model ORDER BY count(*) DESC, model"
    ).fetchall()
    return [r[0] for r in rows]


def overview(con, model: str) -> dict:
    row = con.execute(
        """
        SELECT count(*), count(DISTINCT ticker), count(DISTINCT sector),
               min(fiscal_year), max(fiscal_year), count(*) FILTER (WHERE is_flagged)
        FROM marts.fct_filings WHERE model = ?
        """,
        [model],
    ).fetchone()
    summary = con.execute(
        "SELECT n_documents, task_ok_rate, n_tasks_quarantined, total_cost_usd, total_tokens "
        "FROM marts.mart_model_summary WHERE model = ?",
        [model],
    ).fetchone() or (0, None, 0, 0.0, 0)
    keys = ["filings", "companies", "sectors", "first_year", "last_year", "flagged"]
    more = ["documents", "task_ok_rate", "quarantined", "cost_usd", "tokens"]
    return dict(zip(keys + more, (*row, *summary), strict=True))


def sectors(con) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT sector FROM marts.dim_companies WHERE sector IS NOT NULL ORDER BY 1"
    ).fetchall()
    return [r[0] for r in rows]


def risk_heatmap(con, model: str, sector: str | None = None) -> pd.DataFrame:
    """Share of filings citing each risk category, per fiscal year. For all sectors the
    counts are re-aggregated, so the share is over all filings that year, not an average of
    sector shares."""
    return con.execute(
        """
        SELECT fiscal_year, category, any_value(category_label) AS category_label,
               sum(n_filings_citing) AS n_filings_citing, sum(n_filings) AS n_filings,
               sum(n_filings_citing) / sum(n_filings) AS share
        FROM marts.mart_risk_trends
        WHERE model = $model AND ($sector IS NULL OR sector = $sector)
        GROUP BY fiscal_year, category
        ORDER BY fiscal_year, category
        """,
        {"model": model, "sector": sector},
    ).df()


def risk_examples(con, model: str, category: str, sector: str | None = None, limit: int = 50):
    """The risk titles behind one heatmap row, newest first."""
    return con.execute(
        """
        SELECT ticker, sector, fiscal_year, risk_rank, title
        FROM marts.fct_risk_factors
        WHERE model = $model AND category = $category AND ($sector IS NULL OR sector = $sector)
        ORDER BY fiscal_year DESC, ticker, risk_rank
        LIMIT $limit
        """,
        {"model": model, "category": category, "sector": sector, "limit": limit},
    ).df()


def companies(con) -> pd.DataFrame:
    return con.execute(
        "SELECT ticker, company_name, sector FROM marts.dim_companies ORDER BY ticker"
    ).df()


def company_filings(con, model: str, ticker: str) -> pd.DataFrame:
    return con.execute(
        """
        SELECT fiscal_year, period_end, revenue_usd, net_income_usd, net_margin,
               employee_count, revenue_per_employee_usd, revenue_growth_yoy, n_warnings
        FROM marts.fct_filings
        WHERE model = ? AND ticker = ?
        ORDER BY fiscal_year
        """,
        [model, ticker],
    ).df()


def company_risks(con, model: str, ticker: str) -> pd.DataFrame:
    return con.execute(
        """
        SELECT fiscal_year, risk_rank, title, category_label
        FROM marts.fct_risk_factors
        WHERE model = ? AND ticker = ?
        ORDER BY fiscal_year DESC, risk_rank
        """,
        [model, ticker],
    ).df()


def model_summary(con) -> pd.DataFrame:
    return con.execute("SELECT * FROM marts.mart_model_summary ORDER BY n_records DESC").df()


def eval_scores(con, model: str) -> pd.DataFrame:
    """Field scores from the model's latest evaluation run; empty if none has run yet."""
    if not _has_table(con, "eval", "field_scores"):
        return pd.DataFrame()
    return con.execute(
        """
        WITH latest AS (
            SELECT r.run_id FROM eval.runs AS r
            WHERE r.model = $model ORDER BY r.evaluated_at DESC LIMIT 1
        )
        SELECT reference, field, n, precision, recall, f1, exact_match
        FROM eval.field_scores
        WHERE model = $model AND run_id = (SELECT run_id FROM latest)
          AND (field NOT LIKE 'risk_category:%' OR field = 'risk_category:macro')
        ORDER BY reference DESC, field
        """,
        {"model": model},
    ).df()


def quarantined(con, model: str) -> pd.DataFrame:
    """Tasks that failed validation after every retry, with the reason."""
    return con.execute(
        """
        SELECT d.ticker, d.fiscal_year, t.task, t.attempts, t.quarantine_reason
        FROM staging.stg_task_results AS t
        JOIN (SELECT sha256, min(ticker) AS ticker, min(fiscal_year) AS fiscal_year
              FROM staging.stg_documents GROUP BY sha256) AS d USING (sha256)
        WHERE t.model = ? AND t.status = 'quarantined'
        ORDER BY d.ticker, d.fiscal_year
        """,
        [model],
    ).df()
