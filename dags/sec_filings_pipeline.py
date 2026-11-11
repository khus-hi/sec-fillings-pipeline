"""Weekly SEC 10-K pipeline: EDGAR -> raw zone -> sections -> local LLM -> evaluation -> dbt.

    ingest -> parse -> extract -> evaluate -> dbt_build
                          ^
            check_ollama -+

One task per stage, so a failure retries only that stage and the UI shows where a run
stopped. The work lives in `pipeline/steps.py`; this file only says when the steps run, in
what order, and what happens when they fail. Every stage skips work it has already done
(accession numbers, content hashes, task versions), so a weekly run with no new filings
finishes in seconds and makes no LLM calls.
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.sdk import dag, task


@dag(
    dag_id="sec_filings_pipeline",
    description="10-K filings -> local LLM -> validated tables -> dbt marts, weekly",
    # 10-Ks arrive mostly February to March; weekly is frequent enough and costs nothing
    # when there is nothing new.
    schedule="0 6 * * 1",  # Mondays 06:00 UTC
    start_date=pendulum.datetime(2026, 11, 1, tz="UTC"),
    catchup=False,  # EDGAR always returns the latest filings, so past weeks add nothing
    max_active_runs=1,  # DuckDB allows one writer
    dagrun_timeout=timedelta(hours=8),  # a full re-extraction of 150 filings takes about 40 min
    default_args={
        "owner": "data-eng",
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=30),
    },
    tags=["sec", "edgar", "llm", "dbt"],
    doc_md=__doc__,
)
def sec_filings_pipeline():
    @task(retries=3, retry_delay=timedelta(minutes=1))  # the Ollama app may still be starting
    def check_ollama() -> dict:
        from pipeline.steps import check_ollama as check

        return check()

    @task  # network: EDGAR and the raw zone, so retries help
    def ingest() -> dict:
        from pipeline.steps import ingest_step

        return ingest_step()

    @task(retries=0)  # a parse failure is a parser bug; retrying gives the same result
    def parse(_: dict) -> dict:
        from pipeline.steps import parse_step

        return parse_step()

    @task(execution_timeout=timedelta(hours=6))  # retried when the model server drops out
    def extract(_: dict, __: dict) -> dict:
        from pipeline.steps import extract_step

        return extract_step()

    @task(retries=1)
    def evaluate(_: dict) -> dict:
        from pipeline.steps import evaluate_step

        return evaluate_step()

    @task(retries=0)  # a failing data test is a data problem, not a flaky one
    def dbt_build(_: dict) -> dict:
        from pipeline.steps import dbt_build_step

        return dbt_build_step()

    dbt_build(evaluate(extract(parse(ingest()), check_ollama())))


sec_filings_pipeline()
