"""Print what the manifest holds: python -m ingestion.summary"""

from __future__ import annotations

import duckdb

from ingestion.config import get_settings


def main() -> None:
    con = duckdb.connect(str(get_settings().duckdb_path), read_only=True)
    print("Documents by sector:")
    con.sql(
        """SELECT sector, count(*) AS filings, count(DISTINCT cik) AS companies,
                  min(filing_date) AS first_filed, max(filing_date) AS last_filed,
                  round(sum(size_bytes) / 1e6, 1) AS mb
           FROM meta.documents GROUP BY sector ORDER BY sector"""
    ).show()
    print("Last 5 runs:")
    con.sql(
        """SELECT run_id, started_at, listed, downloaded, skipped, failed,
                  round(bytes_downloaded / 1e6, 1) AS mb, seconds
           FROM meta.ingest_runs ORDER BY started_at DESC LIMIT 5"""
    ).show()


if __name__ == "__main__":
    main()
