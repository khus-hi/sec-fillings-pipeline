"""The pipeline's steps as plain functions, so the Airflow DAG stays a description of order,
schedule and failure policy, and every step can be tested without Airflow.

Each step opens its own DuckDB connection, calls the stage's own `run()`, closes the
connection (DuckDB allows one writer at a time) and returns a small JSON-safe summary, which
Airflow shows as the task's return value. Every stage is idempotent, so a scheduled run that
finds nothing new costs a few seconds and zero LLM calls.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import requests

from ingestion.config import REPO_ROOT, Settings, get_settings, get_store

log = logging.getLogger("pipeline")

DBT_DIR = REPO_ROOT / "dbt"
MAX_ERRORS_SHOWN = 10


class StepFailed(RuntimeError):
    """A step finished but left work undone (a company not ingested, a document not parsed).
    Everything that succeeded is saved, so a retry only redoes what failed."""


def _settings(settings: Settings | None) -> Settings:
    return settings or get_settings()


def check_ollama(url: str | None = None, model: str | None = None, timeout: float = 10) -> dict:
    """Fail fast, before extraction starts, if the model server is down or the model is
    missing, with a message that says what to do."""
    from extraction.llm import DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL

    url = (url or os.getenv("OLLAMA_URL", DEFAULT_OLLAMA_URL)).rstrip("/")
    model = model or os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    try:
        resp = requests.get(f"{url}/api/tags", timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as err:
        raise StepFailed(f"Ollama is not reachable at {url}: start the Ollama app") from err
    names = {m.get("name") for m in resp.json().get("models", [])}
    if model not in names and f"{model}:latest" not in names:
        raise StepFailed(f"model {model!r} is not pulled: run `ollama pull {model}`")
    return {"url": url, "model": model}


def ingest_step(settings: Settings | None = None) -> dict:
    """Download new 10-Ks. Fails if any company could not be ingested (Stage 1 rule: a company
    returning no filings is an error, never a silent gap)."""
    from ingestion.edgar_client import EdgarClient
    from ingestion.ingest import load_companies, run
    from ingestion.manifest import Manifest

    s = _settings(settings)
    client = EdgarClient(s.sec_user_agent, s.max_requests_per_second)
    manifest = Manifest(s.duckdb_path)
    try:
        stats = run(load_companies(), client, get_store(s), manifest, s.filings_per_company)
        summary = {**stats.as_dict(), "documents_total": manifest.count()}
    finally:
        manifest.close()
    if stats.failed:
        raise StepFailed(f"ingest: {stats.failed} failed: {stats.errors[:MAX_ERRORS_SHOWN]}")
    return summary


def parse_step(settings: Settings | None = None) -> dict:
    from ingestion.manifest import Manifest
    from parsing.parse import run

    s = _settings(settings)
    manifest = Manifest(s.duckdb_path)
    try:
        stats = run(manifest.con, get_store(s))
    finally:
        manifest.close()
    errors = stats.pop("errors")
    if stats["failed"]:
        raise StepFailed(f"parse: {stats['failed']} failed: {errors[:MAX_ERRORS_SHOWN]}")
    return stats


def extract_step(settings: Settings | None = None, client=None) -> dict:
    """Run the LLM tasks for documents not yet extracted. A quarantined task is an expected
    outcome (it is stored with its reason and scored as a miss), so it does not fail the step;
    an unreachable model server does, so Airflow retries it."""
    from extraction.extract import run
    from extraction.llm import OllamaClient
    from ingestion.manifest import Manifest

    s = _settings(settings)
    client = client or OllamaClient()
    manifest = Manifest(s.duckdb_path)
    try:
        stats = run(manifest.con, client)  # LLMUnavailable propagates
    finally:
        manifest.close()
    if stats["tasks_quarantined"]:
        log.warning("%d task(s) quarantined; see extracted.quarantine", stats["tasks_quarantined"])
    return {"model": client.model, **stats}


def evaluate_step(settings: Settings | None = None) -> dict:
    """Score every model against the XBRL reference and the verified gold rows."""
    from evaluation.evaluate import run

    s = _settings(settings)
    con = duckdb.connect(str(s.duckdb_path))
    try:
        out = run(con, get_store(s))
    finally:
        con.close()
    f1 = {
        model: {f"{ref}:{field}": round(sc.f1, 3) for (ref, field), sc in res["scores"].items()
                if ":" not in field and sc.f1 is not None}
        for model, res in out["models"].items()
    }  # fmt: skip
    return {"run_id": out["run_id"], "gold_rows": out["gold_rows"], "f1": f1}


def dbt_executable() -> list[str]:
    """DBT_EXECUTABLE (the Airflow image keeps dbt in its own virtualenv), else the dbt next
    to this Python."""
    exe = os.getenv("DBT_EXECUTABLE")
    if exe:
        return [exe]
    local = Path(sys.executable).parent / "dbt"
    return [str(local)] if local.exists() else ["dbt"]


def dbt_build_step(settings: Settings | None = None, target_path: Path | None = None) -> dict:
    """`dbt build` (seed, run, test in dependency order) on the warehouse. Any failing test
    fails the step; the counts come from dbt's run_results.json."""
    s = _settings(settings)
    # Artifacts go to the data folder: writable in the Airflow container and kept between runs.
    target = target_path or s.data_dir / "dbt" / "target"
    env = {**os.environ, "DBT_DUCKDB_PATH": str(s.duckdb_path)}
    cmd = [*dbt_executable(), "build", "--project-dir", str(DBT_DIR),
           "--profiles-dir", str(DBT_DIR), "--target-path", str(target),
           "--log-path", str(target.parent / "logs")]  # fmt: skip
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    seconds = round(time.perf_counter() - t0, 2)
    counts: dict[str, int] = {}
    results_file = target / "run_results.json"
    if results_file.exists():
        for r in json.loads(results_file.read_text())["results"]:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = {"returncode": proc.returncode, "seconds": seconds, **counts}
    log.info("dbt build: %s", summary)
    if proc.returncode != 0:
        tail = "\n".join(proc.stdout.splitlines()[-25:] + proc.stderr.splitlines()[-10:])
        raise StepFailed(f"dbt build failed {summary}\n{tail}")
    return summary
