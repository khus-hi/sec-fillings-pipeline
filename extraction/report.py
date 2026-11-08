"""How well did extraction work? python -m extraction.report

Per task: how many answers validated, how many needed a retry or ended in quarantine,
latency and tokens. Then warnings, the quarantine with reasons, and the extracted values.
"""

from __future__ import annotations

import duckdb

from ingestion.config import get_settings


def main() -> None:
    con = duckdb.connect(str(get_settings().duckdb_path), read_only=True)

    print("Tasks:")
    con.sql(
        """SELECT model, task, count(*) AS docs,
                  count(*) FILTER (WHERE status = 'ok') AS ok,
                  count(*) FILTER (WHERE status = 'quarantined') AS quarantined,
                  count(*) FILTER (WHERE attempts > 1) AS retried,
                  round(avg(latency_s), 1) AS avg_s,
                  round(quantile_cont(latency_s, 0.95), 1) AS p95_s,
                  round(avg(prompt_tokens)) AS avg_prompt_tok,
                  round(avg(completion_tokens)) AS avg_answer_tok,
                  round(sum(cost_usd), 2) AS cost_usd
           FROM extracted.task_results GROUP BY ALL ORDER BY ALL"""
    ).show(max_width=250)

    print("Records (all three tasks ok):")
    con.sql(
        """SELECT model, count(*) AS records,
                  count(*) FILTER (WHERE len(warnings) > 0) AS with_warnings,
                  count(revenue_usd) AS revenue, count(net_income_usd) AS net_income,
                  count(employee_count) AS employees,
                  round(avg(len(risk_factors)), 1) AS avg_risks
           FROM extracted.filings GROUP BY ALL"""
    ).show(max_width=250)

    print("Warnings (by kind):")
    con.sql(
        """SELECT regexp_replace(w, '[-$0-9,.]+', '#', 'g') AS warning, count(*) AS n
           FROM (SELECT unnest(warnings) AS w FROM extracted.filings)
           GROUP BY ALL ORDER BY n DESC"""
    ).show(max_rows=30, max_width=250)

    print("Quarantine:")
    con.sql(
        """SELECT m.ticker, m.filing_date, q.task, q.attempts, left(q.reason, 90) AS reason
           FROM extracted.quarantine q
           JOIN (SELECT sha256, min(ticker) AS ticker, min(filing_date) AS filing_date
                 FROM meta.documents GROUP BY sha256) m USING (sha256)
           ORDER BY m.ticker, m.filing_date"""
    ).show(max_rows=100, max_width=250)

    # Re-checked here in SQL, so records extracted before a rule was added are covered too.
    print("Suspicious financials (net margin over 70%, or revenue under $1B):")
    con.sql(
        """SELECT ticker, fiscal_year, round(revenue_usd / 1e9, 2) AS revenue_bn,
                  round(net_income_usd / 1e9, 2) AS net_income_bn,
                  round(100.0 * net_income_usd / revenue_usd) AS net_margin_pct
           FROM extracted.filings
           WHERE abs(net_income_usd) > 0.7 * revenue_usd OR revenue_usd < 1e9
           ORDER BY ticker, fiscal_year"""
    ).show(max_rows=200, max_width=250)

    print("Risk categories:")
    con.sql(
        """SELECT r.category, count(*) AS n
           FROM (SELECT unnest(risk_factors) AS r FROM extracted.filings)
           GROUP BY ALL ORDER BY n DESC"""
    ).show(max_width=250)

    print("Extracted values ($ billions):")
    con.sql(
        """SELECT ticker, fiscal_year, round(revenue_usd / 1e9, 2) AS revenue_bn,
                  round(net_income_usd / 1e9, 2) AS net_income_bn, employee_count,
                  len(risk_factors) AS risks, len(warnings) AS warnings
           FROM extracted.filings ORDER BY ticker, fiscal_year DESC"""
    ).show(max_rows=200, max_width=250)

    print("Last 5 runs:")
    con.sql(
        """SELECT run_id, model, extractor_version, documents, tasks_ok, tasks_quarantined,
                  tasks_skipped, retries, records, prompt_tokens + completion_tokens AS tokens,
                  cost_usd, seconds
           FROM meta.extract_runs ORDER BY started_at DESC LIMIT 5"""
    ).show(max_width=250)


if __name__ == "__main__":
    main()
