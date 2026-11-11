"""Pipeline steps called by the Airflow DAG (pipeline/steps.py), tested without Airflow."""

from __future__ import annotations

import pytest
import requests
import responses

from extraction.llm import LLMUnavailable
from ingestion.config import Settings
from ingestion.ingest import RunStats
from pipeline import steps

OLLAMA = "http://ollama.test:11434"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        sec_user_agent="Test Runner test@example.com",
        data_dir=tmp_path,
        duckdb_path=tmp_path / "warehouse.duckdb",
        storage_backend="local",
    )


@responses.activate
def test_check_ollama_ok():
    responses.get(f"{OLLAMA}/api/tags", json={"models": [{"name": "qwen3:8b"}]})
    assert steps.check_ollama(OLLAMA, "qwen3:8b") == {"url": OLLAMA, "model": "qwen3:8b"}


@responses.activate
def test_check_ollama_model_missing_says_how_to_fix():
    responses.get(f"{OLLAMA}/api/tags", json={"models": [{"name": "llama3.1:8b"}]})
    with pytest.raises(steps.StepFailed, match="ollama pull qwen3:8b"):
        steps.check_ollama(OLLAMA, "qwen3:8b")


@responses.activate
def test_check_ollama_down_says_how_to_fix():
    responses.get(f"{OLLAMA}/api/tags", body=requests.ConnectionError("refused"))
    with pytest.raises(steps.StepFailed, match="start the Ollama app"):
        steps.check_ollama(OLLAMA, "qwen3:8b")


def test_ingest_step_fails_when_a_company_fails(settings, monkeypatch):
    stats = RunStats(companies=2, listed=3, downloaded=3, failed=1, errors=["XOM: 0 10-K filings"])
    monkeypatch.setattr("ingestion.ingest.run", lambda *a, **k: stats)
    with pytest.raises(steps.StepFailed, match="XOM: 0 10-K filings"):
        steps.ingest_step(settings)


def test_ingest_step_summary(settings, monkeypatch):
    monkeypatch.setattr("ingestion.ingest.run", lambda *a, **k: RunStats(companies=2, skipped=6))
    summary = steps.ingest_step(settings)
    assert summary["skipped"] == 6 and summary["documents_total"] == 0
    assert "errors" not in summary  # the summary goes to Airflow's XCom: keep it small


def test_parse_step_fails_on_a_failed_document(settings, monkeypatch):
    out = {"parsed": 4, "skipped": 0, "failed": 1, "errors": ["AAPL x: ValueError"], "seconds": 1}
    monkeypatch.setattr("parsing.parse.run", lambda *a, **k: dict(out))
    with pytest.raises(steps.StepFailed, match="AAPL x"):
        steps.parse_step(settings)


class _Client:
    model = "fake:1b"


def test_extract_step_quarantine_is_not_a_failure(settings, monkeypatch):
    out = {"documents": 2, "tasks_ok": 5, "tasks_quarantined": 1, "prompt_tokens": 10}
    monkeypatch.setattr("extraction.extract.run", lambda *a, **k: dict(out))
    summary = steps.extract_step(settings, client=_Client())
    assert summary["model"] == "fake:1b" and summary["tasks_quarantined"] == 1


def test_extract_step_model_server_down_propagates(settings, monkeypatch):
    def down(*a, **k):
        raise LLMUnavailable("Ollama is not reachable")

    monkeypatch.setattr("extraction.extract.run", down)
    with pytest.raises(LLMUnavailable):  # Airflow retries the task
        steps.extract_step(settings, client=_Client())


def test_dbt_build_step(settings):
    pytest.importorskip("dbt.cli.main")
    from tests.test_dbt import make_warehouse

    make_warehouse(settings.duckdb_path)
    summary = steps.dbt_build_step(settings)
    assert summary["returncode"] == 0
    assert summary["success"] + summary["pass"] > 40  # models, seed and tests
    assert (settings.data_dir / "dbt" / "target" / "run_results.json").exists()


def test_dbt_build_step_fails_on_a_failing_test(settings):
    pytest.importorskip("dbt.cli.main")
    from tests.test_dbt import make_warehouse

    duplicate = ("AAA", "Technology", 2024, 121_000_000_000, 1, 1200, ["macroeconomic"])
    make_warehouse(settings.duckdb_path, extra_records=[duplicate])
    with pytest.raises(steps.StepFailed, match="dbt build failed"):
        steps.dbt_build_step(settings)
