"""Stage 4 end to end with a scripted fake model: parsed tables in, validated records out."""

import json
from datetime import date

import pytest

from extraction import extract
from extraction.llm import LLMResponse, LLMUnavailable
from ingestion.manifest import Manifest
from ingestion.storage import LocalStore
from parsing import parse
from tests.sample_10k import build_10k
from tests.test_parse_pipeline import _ingest

GOOD = {
    "financials": {
        "revenue": {"value": 391035, "unit": "millions", "quote": "Net sales | $ | 391,035"},
        "net_income": {"value": 93736, "unit": "millions", "quote": "Net income | $ | 93,736"},
    },
    "employees": {
        "employee_count": 164000,
        "quote": "the Company had approximately 164,000 full-time equivalent employees.",
    },
    "risk_factors": {
        "risk_factors": [
            {"title": "Our business depends on global supply chains.", "category": "Supply Chain"}
        ]
    },
}


def _task_of(schema: dict) -> str:
    props = schema["properties"]
    return next(t for t, key in [("financials", "revenue"), ("employees", "employee_count"),
                                 ("risk_factors", "risk_factors")] if key in props)  # fmt: skip


class FakeLLM:
    """Answers each task from a script (a list of raw strings, used in order), then GOOD."""

    model = "fake-model"

    def __init__(self, script: dict[str, list] | None = None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[tuple[str, list[dict]]] = []

    def chat(self, messages, schema):
        task = _task_of(schema)
        self.calls.append((task, messages))
        queued = self.script.get(task)
        content = queued.pop(0) if queued else json.dumps(GOOD[task])
        if isinstance(content, Exception):
            raise content
        return LLMResponse(content, self.model, 0.5, 1000, 50)


@pytest.fixture
def con(tmp_path):
    manifest = Manifest(":memory:")
    store = LocalStore(tmp_path)
    _ingest(manifest, store, "AAA", "0000000001-24-000001", build_10k())
    manifest.con.execute("UPDATE meta.documents SET report_date = DATE '2024-09-28'")
    parse.run(manifest.con, store)
    yield manifest.con
    manifest.close()


def _one(con, sql, *params):
    return con.execute(sql, list(params)).fetchone()


def test_happy_path_stores_tasks_and_record(con):
    llm = FakeLLM()
    stats = extract.run(con, llm)
    assert (stats["documents"], stats["tasks_ok"], stats["records"], stats["retries"]) == (
        1,
        3,
        1,
        0,
    )
    assert stats["prompt_tokens"] == 3000 and stats["cost_usd"] == 0
    rev, ni, emp, fy, risks, warnings = _one(
        con,
        "SELECT revenue_usd, net_income_usd, employee_count, fiscal_year, risk_factors, warnings "
        "FROM extracted.filings",
    )
    assert (rev, ni, emp, fy) == (391_035_000_000, 93_736_000_000, 164_000, 2024)
    assert risks == [{"title": "Our business depends on global supply chains.",
                      "category": "operational_supply_chain"}]  # fmt: skip
    assert warnings == []  # every number and quote was found in its excerpt
    assert _one(con, "SELECT count(*) FROM extracted.quarantine")[0] == 0
    # The employees task saw the Human Capital text, not the start of Item 1.
    employees_prompt = next(m for t, m in llm.calls if t == "employees")[1]["content"]
    assert "164,000" in employees_prompt


def test_invalid_answer_is_retried_with_the_errors(con):
    bad = json.dumps({"employee_count": 164000})  # quote key missing
    llm = FakeLLM({"employees": [bad]})
    stats = extract.run(con, llm)
    assert (stats["tasks_ok"], stats["retries"]) == (3, 1)
    second = [m for t, m in llm.calls if t == "employees"][1]
    assert second[-2] == {"role": "assistant", "content": bad}
    assert "quote: Field required" in second[-1]["content"]
    assert _one(con, "SELECT attempts FROM extracted.task_results WHERE task = 'employees'")[0] == 2


def test_three_failures_go_to_quarantine_and_a_later_run_recovers(con):
    llm = FakeLLM({"financials": ["not json", "{}", '{"revenue": null}']})
    stats = extract.run(con, llm)
    assert (stats["tasks_ok"], stats["tasks_quarantined"], stats["records"]) == (2, 1, 0)
    task, attempts, reason, last = _one(
        con, "SELECT task, attempts, reason, last_output FROM extracted.quarantine"
    )
    assert (task, attempts, last) == ("financials", 3, '{"revenue": null}')
    assert "net_income: Field required" in reason

    stats = extract.run(con, FakeLLM())  # e.g. after a prompt fix
    assert (stats["tasks_ok"], stats["tasks_skipped"], stats["records"]) == (1, 2, 1)
    assert _one(con, "SELECT count(*) FROM extracted.quarantine")[0] == 0


def test_rerun_skips_everything_and_force_redoes_it(con):
    extract.run(con, FakeLLM())
    llm = FakeLLM()
    stats = extract.run(con, llm)
    assert (stats["documents"], llm.calls) == (0, [])
    stats = extract.run(con, FakeLLM(), force=True)
    assert stats["tasks_ok"] == 3
    assert _one(con, "SELECT count(*) FROM extracted.filings")[0] == 1


def test_new_prompt_version_reextracts_only_that_task(con, monkeypatch):
    extract.run(con, FakeLLM())
    monkeypatch.setitem(extract.PROMPT_VERSIONS, "financials", "next")
    llm = FakeLLM()
    stats = extract.run(con, llm)
    assert (stats["tasks_ok"], stats["tasks_skipped"], stats["records"]) == (1, 2, 1)
    assert [t for t, _ in llm.calls] == ["financials"]
    version = _one(con, "SELECT extractor_version FROM extracted.filings")[0]
    assert "financials=s" in version and "-pnext" in version


def test_stored_answer_rejected_by_a_newer_rule_is_extracted_again(con):
    extract.run(con, FakeLLM())
    same = {"value": 36010, "unit": "millions", "quote": "Net income | $ | 93,736"}
    con.execute(  # what a run before the identical-amounts rule could have stored
        "UPDATE extracted.task_results SET result_json = ? WHERE task = 'financials'",
        [json.dumps({"revenue": same, "net_income": same})],
    )
    llm = FakeLLM()
    stats = extract.run(con, llm)
    assert (stats["documents"], stats["tasks_ok"], stats["tasks_skipped"]) == (1, 1, 2)
    assert [t for t, _ in llm.calls] == ["financials"]
    assert _one(con, "SELECT revenue_usd FROM extracted.filings")[0] == 391_035_000_000


def test_ungrounded_values_are_flagged(con):
    made_up = {
        **GOOD["financials"],
        "revenue": {"value": 500000, "unit": "millions", "quote": "Revenue was huge"},
    }
    extract.run(con, FakeLLM({"financials": [json.dumps(made_up)]}))
    warnings = _one(con, "SELECT warnings FROM extracted.filings")[0]
    assert "financials.revenue: value 500000 not found in excerpt" in warnings
    assert "financials.revenue: quote not found in excerpt" in warnings


def test_unexpected_error_quarantines_one_task_but_server_down_stops_the_run(con):
    stats = extract.run(con, FakeLLM({"risk_factors": [RuntimeError("bad gateway")]}))
    assert stats["tasks_quarantined"] == 1
    assert "RuntimeError: bad gateway" in _one(con, "SELECT reason FROM extracted.quarantine")[0]

    with pytest.raises(LLMUnavailable):
        extract.run(con, FakeLLM({"risk_factors": [LLMUnavailable("down")]}))
    finished = _one(con, "SELECT count(*) FROM meta.extract_runs WHERE finished_at IS NULL")[0]
    assert finished == 0  # the run row is closed even when the run stops


def test_missing_period_end_quarantines_the_record(con):
    con.execute("UPDATE meta.documents SET report_date = NULL")
    stats = extract.run(con, FakeLLM())
    assert (stats["tasks_ok"], stats["records"]) == (3, 0)
    task, reason = _one(con, "SELECT task, reason FROM extracted.quarantine")
    assert task == "record" and "period_end" in reason


def test_tasks_and_tickers_filters(con):
    stats = extract.run(con, FakeLLM(), tasks=["employees"])
    assert (stats["tasks_ok"], stats["records"]) == (1, 0)
    assert extract.run(con, FakeLLM(), tickers=["zzz"])["documents"] == 0


def test_period_end_comes_from_manifest_not_the_model(con):
    extract.run(con, FakeLLM())
    record = json.loads(_one(con, "SELECT record_json FROM extracted.filings")[0])
    assert record["period_end"] == str(date(2024, 9, 28)) and record["cik"] == "0000000001"
