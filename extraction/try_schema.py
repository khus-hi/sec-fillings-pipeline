"""Check that the local model can fill each schema: python -m extraction.try_schema AAPL

Sends one relevant chunk per task (employees, financials, risk factors) from the newest
parsed filing of a ticker to Ollama, constrained to that task's JSON schema, and shows
the raw answer, whether it validates, latency and token counts. Local model only, so it
costs nothing. This is a schema check, not the extraction job: Stage 4 adds the real
prompts, chunk selection, retries and storage.
"""

from __future__ import annotations

import argparse
import json
import os
import time

import duckdb
import requests
from pydantic import ValidationError

from extraction.schema import CATEGORY_DESCRIPTIONS, LLM_TASKS, llm_json_schema
from ingestion.config import get_settings

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_URL = "http://localhost:11434"

# Which section, and which words, locate a useful chunk for each task.
_CHUNK_QUERY = {
    "employees": ("item_1", ["employees"]),
    "financials": ("item_8", ["net income", "total"]),
    "risk_factors": ("item_1a", []),
}

_INSTRUCTIONS = {
    "employees": "Extract the company's total employee count.",
    "financials": "Extract total revenue and net income for the most recent fiscal year "
    "from this excerpt. Copy numbers as printed and give the unit stated for the table.",
    "risk_factors": "List the most significant risk factors in this excerpt and give each "
    "one category. Categories:\n"
    + "\n".join(f"- {c.value}: {d}" for c, d in CATEGORY_DESCRIPTIONS.items()),
}


def find_chunk(con: duckdb.DuckDBPyConnection, ticker: str, task: str) -> tuple[str, str]:
    section, words = _CHUNK_QUERY[task]
    where = "".join(" AND c.text ILIKE ?" for _ in words)
    row = con.execute(
        f"""
        SELECT c.chunk_id, c.text
        FROM parsed.chunks c
        JOIN meta.documents m USING (sha256)
        WHERE m.ticker = ? AND c.section = ? {where}
        ORDER BY m.filing_date DESC, c.chunk_index
        LIMIT 1
        """,
        [ticker.upper(), section, *[f"%{w}%" for w in words]],
    ).fetchone()
    if not row:
        raise SystemExit(f"no {section} chunk for {ticker} (run python -m parsing.parse first)")
    return row


def ask(url: str, model: str, task: str, text: str) -> dict:
    """One constrained call to Ollama's chat API."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You extract facts from SEC 10-K filings. "
             "Answer only from the excerpt. Use null when a value is not in it."},
            {"role": "user", "content": f"{_INSTRUCTIONS[task]}\n\nEXCERPT:\n{text}"},
        ],
        "format": llm_json_schema(task),
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_ctx": 8192},
    }  # fmt: skip
    resp = requests.post(f"{url}/api/chat", json=body, timeout=600)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("ticker")
    p.add_argument("--tasks", nargs="+", choices=list(LLM_TASKS), default=list(LLM_TASKS))
    p.add_argument("--model", default=os.getenv("OLLAMA_MODEL", DEFAULT_MODEL))
    args = p.parse_args()
    url = os.getenv("OLLAMA_URL", DEFAULT_URL)

    con = duckdb.connect(str(get_settings().duckdb_path), read_only=True)
    passed = 0
    for task in args.tasks:
        chunk_id, text = find_chunk(con, args.ticker, task)
        t0 = time.perf_counter()
        try:
            reply = ask(url, args.model, task, text)
        except requests.ConnectionError:
            raise SystemExit(f"Ollama is not reachable at {url}: open the Ollama app") from None
        seconds = time.perf_counter() - t0
        raw = reply["message"]["content"]
        print(f"\n== {task}  chunk {chunk_id}  {len(text):,} chars")
        print(
            f"   {args.model}: {seconds:.1f}s, prompt {reply.get('prompt_eval_count')} tokens, "
            f"answer {reply.get('eval_count')} tokens"
        )
        try:
            parsed = LLM_TASKS[task].model_validate_json(raw)
            passed += 1
            print("   VALID")
            print(json.dumps(parsed.model_dump(mode="json"), indent=2))
        except ValidationError as err:
            print(f"   INVALID ({err.error_count()} errors)\n   raw: {raw[:1000]}\n{err}")
    print(f"\n{passed}/{len(args.tasks)} answers validated")


if __name__ == "__main__":
    main()
