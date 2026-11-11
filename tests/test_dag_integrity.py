"""The Airflow DAG imports cleanly and is wired as documented.

Needs Airflow, so it is skipped in the normal dev environment and runs in its own CI job
against the Airflow version in the Astro image (see .github/workflows/ci.yml).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("airflow")

DAGS_DIR = Path(__file__).resolve().parent.parent / "dags"


@pytest.fixture(scope="module")
def dagbag():
    from airflow.models import DagBag

    # Only our folder is scanned, so Airflow's example DAGs never load here.
    return DagBag(dag_folder=str(DAGS_DIR))


def test_no_import_errors(dagbag):
    assert dagbag.import_errors == {}


def test_sec_filings_pipeline_shape(dagbag):
    dag = dagbag.dags.get("sec_filings_pipeline")  # .dags: no metadata database needed
    assert dag is not None
    assert dag.max_active_runs == 1  # DuckDB has a single writer
    assert dag.catchup is False

    tasks = {t.task_id: t for t in dag.tasks}
    assert set(tasks) == {"check_ollama", "ingest", "parse", "extract", "evaluate", "dbt_build"}
    assert tasks["ingest"].downstream_task_ids == {"parse"}
    assert tasks["parse"].downstream_task_ids == {"extract"}
    assert tasks["check_ollama"].downstream_task_ids == {"extract"}
    assert tasks["extract"].downstream_task_ids == {"evaluate"}
    assert tasks["evaluate"].downstream_task_ids == {"dbt_build"}
    assert tasks["dbt_build"].downstream_task_ids == set()

    assert tasks["parse"].retries == 0  # a parser bug is not fixed by retrying
    assert tasks["dbt_build"].retries == 0  # nor is a failing data test
    assert tasks["extract"].retries == 2  # the model server can drop out
    assert tasks["extract"].execution_timeout == timedelta(hours=6)
