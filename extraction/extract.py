"""Stage 4: parsed 10-K sections -> LLM -> validated records, stored in DuckDB.

    python -m extraction.extract --limit 5          # try a few filings first
    python -m extraction.extract                    # everything not yet extracted
    python -m extraction.extract --tickers AAPL --force

For each filing, three LLM tasks run (financials, employees, risk_factors). Each task:

1. gets its own excerpt (extraction/context.py),
2. is answered under its JSON schema (constrained decoding),
3. is validated with Pydantic; on failure the errors are sent back to the model and it
   tries again, up to MAX_ATTEMPTS,
4. lands in `extracted.task_results` (ok) or `extracted.quarantine` (with the reason and
   the last raw answer), never silently dropped.

When all three tasks are ok, a `FilingRecord` is assembled with the manifest facts and
stored in `extracted.filings`, with sanity and grounding warnings attached.

Idempotency: a task is skipped when it is already ok for the same document hash, model and
its task version (schema version + that task's prompt version), so changing one prompt re-runs
only that task. Each run is logged in `meta.extract_runs`, and
every task writes one JSON log line with model, latency, tokens, cost, attempts and result.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import uuid
from dataclasses import dataclass, field

import duckdb
from pydantic import BaseModel, ValidationError

from extraction import context as ctx
from extraction.llm import LLMClient, LLMUnavailable, OllamaClient
from extraction.prompts import PROMPT_VERSIONS, build_messages, retry_message
from extraction.schema import (
    LLM_TASKS,
    SCHEMA_VERSION,
    FilingRecord,
    llm_json_schema,
    sanity_warnings,
)
from ingestion.config import get_settings
from ingestion.manifest import Manifest, utcnow

log = logging.getLogger("extraction")

MAX_ATTEMPTS = 3
TASKS = list(LLM_TASKS)


def task_version(task: str) -> str:
    """Cache key for one task's answers: schema version + that task's prompt version."""
    return f"s{SCHEMA_VERSION}-p{PROMPT_VERSIONS.get(task, '0')}"


def extractor_version() -> str:
    """All task versions, stored with runs and records."""
    return ",".join(f"{t}={task_version(t)}" for t in TASKS)


DDL = """
CREATE SCHEMA IF NOT EXISTS extracted;

CREATE TABLE IF NOT EXISTS extracted.task_results (
    sha256            VARCHAR NOT NULL,
    task              VARCHAR NOT NULL,      -- financials | employees | risk_factors
    model             VARCHAR NOT NULL,
    extractor_version VARCHAR NOT NULL,
    status            VARCHAR NOT NULL,      -- ok | quarantined
    attempts          INTEGER NOT NULL,
    result_json       VARCHAR,               -- validated answer (ok only)
    warnings          VARCHAR[],
    context_chars     INTEGER,
    latency_s         DOUBLE,                -- summed over attempts
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cost_usd          DOUBLE,
    extracted_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (sha256, task, model)
);

CREATE TABLE IF NOT EXISTS extracted.quarantine (
    sha256            VARCHAR NOT NULL,
    task              VARCHAR NOT NULL,      -- an LLM task, or "record" for assembly
    model             VARCHAR NOT NULL,
    extractor_version VARCHAR NOT NULL,
    attempts          INTEGER NOT NULL,
    reason            VARCHAR NOT NULL,
    last_output       VARCHAR,
    quarantined_at    TIMESTAMP NOT NULL,
    PRIMARY KEY (sha256, task, model)
);

CREATE TABLE IF NOT EXISTS extracted.filings (
    sha256            VARCHAR NOT NULL,
    model             VARCHAR NOT NULL,
    accession_number  VARCHAR NOT NULL,
    cik               VARCHAR NOT NULL,
    ticker            VARCHAR NOT NULL,
    company_name      VARCHAR,
    period_end        DATE NOT NULL,
    fiscal_year       INTEGER NOT NULL,
    revenue_usd       BIGINT,
    net_income_usd    BIGINT,
    employee_count    INTEGER,
    risk_factors      STRUCT(title VARCHAR, category VARCHAR)[],
    warnings          VARCHAR[],
    record_json       VARCHAR NOT NULL,
    extractor_version VARCHAR NOT NULL,
    extracted_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (sha256, model)
);

CREATE TABLE IF NOT EXISTS meta.extract_runs (
    run_id            VARCHAR PRIMARY KEY,
    model             VARCHAR NOT NULL,
    extractor_version VARCHAR NOT NULL,
    started_at        TIMESTAMP NOT NULL,
    finished_at       TIMESTAMP,
    documents         INTEGER,
    tasks_ok          INTEGER,
    tasks_quarantined INTEGER,
    tasks_skipped     INTEGER,
    retries           INTEGER,
    records           INTEGER,
    prompt_tokens     BIGINT,
    completion_tokens BIGINT,
    cost_usd          DOUBLE,
    seconds           DOUBLE
);
"""


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(DDL)


# ---------- Inputs ----------


@dataclass
class Document:
    sha256: str
    accession_number: str
    cik: str
    ticker: str
    company_name: str | None
    period_end: object  # date | None
    parser_version: str


def pending_documents(
    con: duckdb.DuckDBPyConnection,
    model: str,
    tickers: list[str] | None,
    force: bool,
) -> list[Document]:
    """Parsed documents with at least one task that has no current, valid answer."""
    where, params = "", []
    if tickers:
        where = f"AND m.ticker IN ({', '.join('?' for _ in tickers)})"
        params = [t.upper() for t in tickers]
    rows = con.execute(
        f"""
        SELECT p.sha256, min(m.accession_number), min(m.cik), min(m.ticker),
               min(m.company_name), min(m.report_date), min(p.parser_version)
        FROM parsed.documents p JOIN meta.documents m USING (sha256)
        WHERE p.status = 'ok' {where}
        GROUP BY p.sha256
        ORDER BY min(m.ticker), min(m.filing_date) DESC
        """,
        params,
    ).fetchall()
    docs = [Document(*r) for r in rows]
    if force:
        return docs
    # Checked in Python with the same rule the run uses (load_answers), so "done" always
    # means: current task version and still valid under today's schema.
    return [d for d in docs if len(load_answers(con, d.sha256, model)) < len(TASKS)]


def load_context(con: duckdb.DuckDBPyConnection, sha256: str, task: str) -> str:
    def section(name: str) -> str:
        row = con.execute(
            "SELECT text FROM parsed.sections WHERE sha256 = ? AND section = ?", [sha256, name]
        ).fetchone()
        return row[0] if row else ""

    def chunks(name: str) -> list[str]:
        rows = con.execute(
            "SELECT text FROM parsed.chunks WHERE sha256 = ? AND section = ? ORDER BY chunk_index",
            [sha256, name],
        ).fetchall()
        return [r[0] for r in rows]

    if task == "employees":
        return ctx.employees_context(section("item_1"))
    if task == "financials":
        return ctx.financials_context(chunks("item_8"), chunks("item_7"))
    if task == "risk_factors":
        return ctx.risk_context(section("item_1a"))
    raise ValueError(task)


# ---------- Grounding: is the answer actually in the excerpt? ----------


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _number_in_text(value: float, text: str) -> bool:
    digits = f"{abs(value):f}".rstrip("0").rstrip(".")
    return digits in text.replace(",", "")


def grounding_warnings(task: str, answer: BaseModel, context: str) -> list[str]:
    """Cheap hallucination checks. A number or quote that isn't in the excerpt was not read
    from it. These are warnings, not rejections: a model may legitimately reformat a quote."""
    warnings, squashed = [], _squash(context)

    def check_quote(name: str, quote: str | None) -> None:
        if quote and _squash(quote) not in squashed:
            warnings.append(f"{task}.{name}: quote not found in excerpt")

    if task == "financials":
        for name in ("revenue", "net_income"):
            amount = getattr(answer, name)
            if amount is None:
                continue
            if not _number_in_text(amount.value, context):
                warnings.append(f"{task}.{name}: value {amount.value:g} not found in excerpt")
            check_quote(name, amount.quote)
    elif task == "employees":
        if answer.employee_count is not None and not _number_in_text(
            answer.employee_count, context
        ):
            warnings.append(f"{task}: count {answer.employee_count} not found in excerpt")
        check_quote("quote", answer.quote)
    return warnings


# ---------- One task, with retries ----------


@dataclass
class TaskOutcome:
    ok: bool
    answer: BaseModel | None
    attempts: int
    reason: str | None = None
    last_output: str | None = None
    warnings: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


def _errors(err: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc']) or 'answer'}: {e['msg']}" for e in err.errors()]


def run_task(client: LLMClient, task: str, doc: Document, context: str) -> TaskOutcome:
    model_cls = LLM_TASKS[task]
    if not context.strip():
        return TaskOutcome(False, None, 0, reason="no text to extract from (section missing)")
    messages = build_messages(task, context, doc.company_name or doc.ticker, str(doc.period_end))
    schema = llm_json_schema(task)
    out = TaskOutcome(False, None, 0)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp = client.chat(messages, schema)
        out.attempts = attempt
        out.latency_s += resp.latency_s
        out.prompt_tokens += resp.prompt_tokens
        out.completion_tokens += resp.completion_tokens
        out.cost_usd += resp.cost_usd
        out.last_output = resp.content
        try:
            answer = model_cls.model_validate_json(resp.content)
        except ValidationError as err:
            errors = _errors(err)
            out.reason = "; ".join(errors)[:1000]
            messages = [
                *messages,
                {"role": "assistant", "content": resp.content},
                retry_message(errors),
            ]
            continue
        out.ok, out.answer, out.reason = True, answer, None
        out.warnings = grounding_warnings(task, answer, context)
        return out
    return out


# ---------- Storage ----------


def save_task(con, doc: Document, task: str, model: str, out: TaskOutcome, context_chars: int):
    now = utcnow()
    con.execute(
        "DELETE FROM extracted.task_results WHERE sha256 = ? AND task = ? AND model = ?",
        [doc.sha256, task, model],
    )
    con.execute(
        "INSERT INTO extracted.task_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            doc.sha256, task, model, task_version(task), "ok" if out.ok else "quarantined",
            out.attempts, out.answer.model_dump_json() if out.answer else None, out.warnings,
            context_chars, round(out.latency_s, 3), out.prompt_tokens, out.completion_tokens,
            out.cost_usd, now,
        ],
    )  # fmt: skip
    quarantine(con, doc.sha256, task, model, out.attempts, out.reason, out.last_output, out.ok)


def quarantine(con, sha256, task, model, attempts, reason, last_output, resolved=False):
    con.execute(
        "DELETE FROM extracted.quarantine WHERE sha256 = ? AND task = ? AND model = ?",
        [sha256, task, model],
    )
    if not resolved:
        con.execute(
            "INSERT INTO extracted.quarantine VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [sha256, task, model, task_version(task), attempts, reason or "unknown",
             last_output, utcnow()],
        )  # fmt: skip


def load_answers(con, sha256: str, model: str) -> dict[str, BaseModel]:
    """Stored answers that are current for their task. Each is validated again, so an answer
    that a newer, stricter rule rejects counts as not done and is extracted again."""
    rows = con.execute(
        "SELECT task, extractor_version, result_json FROM extracted.task_results "
        "WHERE sha256 = ? AND model = ? AND status = 'ok'",
        [sha256, model],
    ).fetchall()
    answers = {}
    for task, version, js in rows:
        if task in LLM_TASKS and version == task_version(task):
            try:
                answers[task] = LLM_TASKS[task].model_validate_json(js)
            except ValidationError:
                continue
    return answers


def save_record(con, doc: Document, model: str, answers: dict[str, BaseModel]) -> FilingRecord:
    record = FilingRecord(
        accession_number=doc.accession_number,
        sha256=doc.sha256,
        cik=doc.cik,
        ticker=doc.ticker,
        company_name=doc.company_name,
        period_end=doc.period_end,
        financials=answers["financials"],
        employees=answers["employees"],
        risks=answers["risk_factors"],
        model=model,
        parser_version=doc.parser_version,
        extracted_at=utcnow(),
    )
    rows = con.execute(
        "SELECT task, extractor_version, warnings FROM extracted.task_results "
        "WHERE sha256 = ? AND model = ? AND status = 'ok'",
        [doc.sha256, model],
    ).fetchall()
    task_warnings = [w for t, v, ws in rows if v == task_version(t) for w in (ws or [])]
    warnings = sanity_warnings(record) + list(task_warnings)
    con.execute("DELETE FROM extracted.filings WHERE sha256 = ? AND model = ?", [doc.sha256, model])
    con.execute(
        "INSERT INTO extracted.filings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            doc.sha256, model, record.accession_number, record.cik, record.ticker,
            record.company_name, record.period_end, record.fiscal_year, record.revenue_usd,
            record.net_income_usd, record.employees.employee_count,
            [{"title": r.title, "category": r.category.value} for r in record.risks.risk_factors],
            warnings, record.model_dump_json(), extractor_version(), record.extracted_at,
        ],
    )  # fmt: skip
    return record


# ---------- The run ----------


def run(
    con: duckdb.DuckDBPyConnection,
    client: LLMClient,
    tickers: list[str] | None = None,
    tasks: list[str] | None = None,
    force: bool = False,
    limit: int | None = None,
) -> dict:
    ensure_schema(con)
    tasks = tasks or TASKS
    model = client.model
    run_id = uuid.uuid4().hex[:12]
    con.execute(
        "INSERT INTO meta.extract_runs (run_id, model, extractor_version, started_at) "
        "VALUES (?, ?, ?, ?)",
        [run_id, model, extractor_version(), utcnow()],
    )
    docs = pending_documents(con, model, tickers, force)
    if limit is not None:
        docs = docs[:limit]
    stats = dict(documents=0, tasks_ok=0, tasks_quarantined=0, tasks_skipped=0, retries=0,
                 records=0, prompt_tokens=0, completion_tokens=0, cost_usd=0.0)  # fmt: skip
    started = time.perf_counter()

    try:
        for doc in docs:
            stats["documents"] += 1
            done = set() if force else set(load_answers(con, doc.sha256, model))
            for task in tasks:
                if task in done:
                    stats["tasks_skipped"] += 1
                    continue
                context = load_context(con, doc.sha256, task)
                try:
                    out = run_task(client, task, doc, context)
                except LLMUnavailable:
                    raise  # the server is down: stop, don't quarantine every document
                except Exception as err:  # one bad call must not stop the run
                    out = TaskOutcome(False, None, 1, reason=f"{type(err).__name__}: {err}")
                save_task(con, doc, task, model, out, len(context))
                stats["tasks_ok" if out.ok else "tasks_quarantined"] += 1
                stats["retries"] += max(out.attempts - 1, 0)
                stats["prompt_tokens"] += out.prompt_tokens
                stats["completion_tokens"] += out.completion_tokens
                stats["cost_usd"] += out.cost_usd
                log.info(json.dumps({
                    "event": "task", "ticker": doc.ticker, "accession": doc.accession_number,
                    "task": task, "model": model, "valid": out.ok, "attempts": out.attempts,
                    "latency_s": round(out.latency_s, 2), "prompt_tokens": out.prompt_tokens,
                    "completion_tokens": out.completion_tokens, "cost_usd": out.cost_usd,
                    "context_chars": len(context), "warnings": out.warnings,
                    "error": out.reason if not out.ok else None,
                }))  # fmt: skip

            answers = load_answers(con, doc.sha256, model)
            if set(answers) >= set(TASKS):
                try:
                    record = save_record(con, doc, model, answers)
                    stats["records"] += 1
                    quarantine(con, doc.sha256, "record", model, 0, None, None, resolved=True)
                    log.info(json.dumps({
                        "event": "record", "ticker": doc.ticker, "fiscal_year": record.fiscal_year,
                        "revenue_usd": record.revenue_usd, "net_income_usd": record.net_income_usd,
                        "employees": record.employees.employee_count,
                        "risks": len(record.risks.risk_factors),
                    }))  # fmt: skip
                except ValidationError as err:
                    quarantine(con, doc.sha256, "record", model, 0, "; ".join(_errors(err)), None)
                    log.warning("record for %s %s quarantined: %s", doc.ticker,
                                doc.accession_number, err)  # fmt: skip
            else:
                # A task failed this time: drop the record built from older answers, so a
                # stale row never sits in the table looking like a current result.
                con.execute(
                    "DELETE FROM extracted.filings WHERE sha256 = ? AND model = ?",
                    [doc.sha256, model],
                )
    finally:
        stats["seconds"] = round(time.perf_counter() - started, 2)
        con.execute(
            "UPDATE meta.extract_runs SET finished_at = ?, documents = ?, tasks_ok = ?, "
            "tasks_quarantined = ?, tasks_skipped = ?, retries = ?, records = ?, "
            "prompt_tokens = ?, completion_tokens = ?, cost_usd = ?, seconds = ? WHERE run_id = ?",
            [utcnow(), stats["documents"], stats["tasks_ok"], stats["tasks_quarantined"],
             stats["tasks_skipped"], stats["retries"], stats["records"], stats["prompt_tokens"],
             stats["completion_tokens"], stats["cost_usd"], stats["seconds"], run_id],
        )  # fmt: skip
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extract structured fields from parsed 10-Ks.")
    p.add_argument("--tickers", nargs="+", help="only these tickers")
    p.add_argument("--tasks", nargs="+", choices=TASKS, help="only these tasks")
    p.add_argument("--limit", type=int, help="at most this many documents")
    p.add_argument("--force", action="store_true", help="re-extract tasks that are already ok")
    p.add_argument("--model", help="Ollama model (default: OLLAMA_MODEL or qwen3:8b)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = get_settings()
    manifest = Manifest(settings.duckdb_path)
    client = OllamaClient(model=args.model)
    try:
        stats = run(manifest.con, client, args.tickers, args.tasks, args.force, args.limit)
    except LLMUnavailable as err:
        print(f"\nstopped: {err}")
        return 2
    finally:
        manifest.close()

    tokens = stats["prompt_tokens"] + stats["completion_tokens"]
    print(
        f"\n{client.model}: {stats['documents']} documents | tasks ok {stats['tasks_ok']}, "
        f"quarantined {stats['tasks_quarantined']}, skipped {stats['tasks_skipped']} | "
        f"retries {stats['retries']} | records {stats['records']} | {tokens:,} tokens | "
        f"${stats['cost_usd']:.2f} | {stats['seconds']}s"
    )
    return 1 if stats["tasks_quarantined"] else 0


if __name__ == "__main__":
    sys.exit(main())
