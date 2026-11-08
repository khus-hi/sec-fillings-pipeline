import json

import pytest
import requests
import responses

from extraction.llm import LLMUnavailable, OllamaClient

URL = "http://ollama.test:11434"


@responses.activate
def test_chat_sends_schema_and_returns_tokens():
    responses.post(
        f"{URL}/api/chat",
        json={"message": {"content": '{"a": 1}'}, "prompt_eval_count": 812, "eval_count": 9},
    )
    client = OllamaClient(model="qwen3:8b", url=URL)
    resp = client.chat([{"role": "user", "content": "hi"}], {"type": "object"})
    sent = json.loads(responses.calls[0].request.body)
    assert sent["format"] == {"type": "object"} and sent["think"] is False
    assert sent["options"] == {"temperature": 0, "num_ctx": 8192}
    assert (resp.content, resp.prompt_tokens, resp.completion_tokens, resp.cost_usd) == (
        '{"a": 1}',
        812,
        9,
        0.0,
    )
    assert resp.latency_s >= 0


@responses.activate
def test_unreachable_server_and_missing_model_raise_unavailable():
    client = OllamaClient(model="nope:1b", url=URL)
    responses.post(f"{URL}/api/chat", body=requests.ConnectionError("refused"))
    with pytest.raises(LLMUnavailable, match="not reachable"):
        client.chat([], {})
    responses.replace(responses.POST, f"{URL}/api/chat", status=404, json={"error": "x"})
    with pytest.raises(LLMUnavailable, match="ollama pull nope:1b"):
        client.chat([], {})
