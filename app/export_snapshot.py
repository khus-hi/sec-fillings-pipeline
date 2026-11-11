"""Copy what the Streamlit app reads into a small, read-only snapshot for the hosted demo.

    python -m app.export_snapshot

The real warehouse (data/warehouse.duckdb) is local and gitignored. The hosted app on
Streamlit Community Cloud can't run the pipeline, so it reads this snapshot instead: the dbt
marts, the evaluation scores and the few staging columns the quality tab needs. Nothing raw
(filing text, chunks, model output) is copied.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from ingestion.config import REPO_ROOT, get_settings

SNAPSHOT_PATH = REPO_ROOT / "app" / "snapshot" / "warehouse.duckdb"

# schema -> {table: columns to keep ("*" for all)}
TABLES = {
    "marts": {
        "dim_companies": "*",
        "fct_filings": "*",
        "fct_risk_factors": "*",
        "mart_risk_trends": "*",
        "mart_model_summary": "*",
    },
    "eval": {"runs": "*", "field_scores": "*"},
    "staging": {
        "stg_documents": "sha256, ticker, fiscal_year",
        "stg_task_results": "sha256, task, model, status, attempts, quarantine_reason",
    },
}


def export(source: Path, target: Path = SNAPSHOT_PATH) -> dict[str, int]:
    """Write the snapshot from scratch and return the row count of each table."""
    if not source.exists():
        raise FileNotFoundError(f"No warehouse at {source}. Run the pipeline first.")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    counts: dict[str, int] = {}
    con = duckdb.connect(str(tmp))
    try:
        con.execute(f"ATTACH '{source}' AS src (READ_ONLY)")
        for schema, tables in TABLES.items():
            con.execute(f"CREATE SCHEMA {schema}")
            for table, cols in tables.items():
                con.execute(
                    f"CREATE TABLE {schema}.{table} AS SELECT {cols} FROM src.{schema}.{table}"
                )
                counts[f"{schema}.{table}"] = con.execute(
                    f"SELECT count(*) FROM {schema}.{table}"
                ).fetchone()[0]
        con.execute("DETACH src")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    tmp.replace(target)  # atomic: the app never sees a half-written snapshot
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=get_settings().duckdb_path)
    parser.add_argument("--target", type=Path, default=SNAPSHOT_PATH)
    args = parser.parse_args()
    for name, n in export(args.source, args.target).items():
        print(f"{name:32} {n:>7,}")
    size_mb = args.target.stat().st_size / 1e6
    print(f"Wrote {args.target} ({size_mb:.1f} MB). Commit it to update the hosted app.")


if __name__ == "__main__":
    main()
