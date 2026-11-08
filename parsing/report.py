"""How well did parsing work? python -m parsing.report

Prints section coverage (how many documents each Item was found in, and how many of
those look too short to be the real section), chunk sizes, and the documents with
problems, so failures are visible instead of silent.
"""

from __future__ import annotations

import duckdb

from ingestion.config import get_settings


def main() -> None:
    con = duckdb.connect(str(get_settings().duckdb_path), read_only=True)

    print("Parse status:")
    con.sql(
        """SELECT parser_version, status, count(*) AS docs,
                  round(sum(text_chars) / 1e6, 1) AS text_mchars,
                  round(avg(seconds), 2) AS avg_seconds
           FROM parsed.documents GROUP BY ALL ORDER BY ALL"""
    ).show()

    print("Section coverage (ok documents):")
    con.sql(
        """WITH docs AS (SELECT count(*) AS n FROM parsed.documents WHERE status = 'ok')
           SELECT s.section,
                  count(*) AS found,
                  (SELECT n FROM docs) AS of_docs,
                  round(100.0 * count(*) / (SELECT n FROM docs), 1) AS pct_found,
                  count(*) FILTER (WHERE s.is_short) AS short,
                  count(*) FILTER (WHERE s.found_by = 'title') AS by_title,
                  median(s.char_count)::BIGINT AS median_chars,
                  max(s.char_count) AS max_chars
           FROM parsed.sections s
           JOIN parsed.documents d USING (sha256)
           WHERE d.status = 'ok'
           GROUP BY s.section ORDER BY s.section"""
    ).show()

    print("Chunks:")
    con.sql(
        """SELECT section, count(*) AS chunks,
                  round(avg(approx_tokens)) AS avg_tokens, max(approx_tokens) AS max_tokens
           FROM parsed.chunks GROUP BY section ORDER BY section"""
    ).show()

    print("Documents missing or with a short Item 1A / Item 7 / Item 8:")
    con.sql(
        """SELECT m.ticker, m.filing_date, p.sections_missing AS missing, p.sections_short AS short
           FROM parsed.documents p JOIN meta.documents m USING (sha256)
           WHERE p.status = 'ok'
             AND (list_has_any(p.sections_missing, ['item_1a', 'item_7', 'item_8'])
                  OR list_has_any(p.sections_short, ['item_1a', 'item_7', 'item_8']))
           ORDER BY m.ticker, m.filing_date"""
    ).show(max_rows=200)

    failed = con.sql("SELECT accession_number, error FROM parsed.documents WHERE status = 'failed'")
    if failed.fetchall():
        print("Failed documents:")
        failed.show()

    print("Last 5 parse runs:")
    con.sql(
        """SELECT run_id, parser_version, started_at, parsed, skipped, failed, seconds
           FROM meta.parse_runs ORDER BY started_at DESC LIMIT 5"""
    ).show()


if __name__ == "__main__":
    main()
