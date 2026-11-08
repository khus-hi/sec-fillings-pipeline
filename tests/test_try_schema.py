import json

import duckdb
import pytest
import responses

from extraction import try_schema
from extraction.schema import EmployeesExtraction


@pytest.fixture
def con():
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA meta; CREATE SCHEMA parsed")
    con.execute("CREATE TABLE meta.documents (sha256 VARCHAR, ticker VARCHAR, filing_date DATE)")
    con.execute(
        "CREATE TABLE parsed.chunks (chunk_id VARCHAR, sha256 VARCHAR, section VARCHAR, "
        "chunk_index INTEGER, text VARCHAR)"
    )
    con.execute("INSERT INTO meta.documents VALUES ('old', 'AAPL', '2023-11-03'), "
                "('new', 'AAPL', '2024-11-01')")  # fmt: skip
    con.execute(
        "INSERT INTO parsed.chunks VALUES "
        "('new:item_1:000', 'new', 'item_1', 0, 'Products and services.'), "
        "('new:item_1:001', 'new', 'item_1', 1, 'We had about 164,000 Employees.'), "
        "('old:item_1:001', 'old', 'item_1', 1, 'We had about 161,000 employees.')"
    )
    return con


def test_find_chunk_picks_newest_filing_and_matching_text(con):
    chunk_id, text = try_schema.find_chunk(con, "aapl", "employees")
    assert chunk_id == "new:item_1:001" and "164,000" in text


def test_find_chunk_explains_missing_parse(con):
    with pytest.raises(SystemExit, match="no item_8 chunk"):
        try_schema.find_chunk(con, "AAPL", "financials")


@responses.activate
def test_ask_sends_schema_and_disables_thinking():
    answer = {"employee_count": 164000, "quote": "approximately 164,000"}
    responses.post(
        "http://ollama:11434/api/chat",
        json={"message": {"content": json.dumps(answer)}, "eval_count": 20},
    )
    reply = try_schema.ask("http://ollama:11434", "qwen3:8b", "employees", "text")
    sent = json.loads(responses.calls[0].request.body)
    assert sent["format"]["properties"]["employee_count"]
    assert sent["think"] is False and sent["options"]["temperature"] == 0
    parsed = EmployeesExtraction.model_validate_json(reply["message"]["content"])
    assert parsed.employee_count == 164000
