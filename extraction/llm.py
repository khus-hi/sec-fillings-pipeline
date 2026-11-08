"""LLM clients. The extraction job talks to the `LLMClient` protocol, so a hosted model
can be added for the comparison stage without touching the job.

Every call returns latency, token counts and cost alongside the text, because those are
logged per document. A local Ollama model costs $0.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:8b"


class LLMUnavailable(RuntimeError):
    """The model server can't be reached: stop the run instead of quarantining every task."""


@dataclass(frozen=True)
class LLMResponse:
    content: str
    model: str
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float = 0.0


class LLMClient(Protocol):
    model: str

    def chat(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> LLMResponse: ...


class OllamaClient:
    """Ollama's /api/chat with the JSON schema as `format` (constrained decoding)."""

    def __init__(
        self,
        model: str | None = None,
        url: str | None = None,
        num_ctx: int = 8192,
        timeout: float = 600,
        session: requests.Session | None = None,
    ):
        self.model = model or os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
        self.url = (url or os.getenv("OLLAMA_URL", DEFAULT_OLLAMA_URL)).rstrip("/")
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.session = session or requests.Session()
        # Retry the server's own hiccups (e.g. 503 while a model loads), never a 4xx.
        retry = Retry(total=2, backoff_factor=2.0, status_forcelist=(500, 502, 503, 504),
                      allowed_methods=("POST",))  # fmt: skip
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def chat(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> LLMResponse:
        body = {
            "model": self.model,
            "messages": messages,
            "format": schema,
            "stream": False,
            "think": False,  # qwen3 would otherwise spend tokens reasoning before answering
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }
        t0 = time.perf_counter()
        try:
            resp = self.session.post(f"{self.url}/api/chat", json=body, timeout=self.timeout)
        except requests.ConnectionError as err:
            raise LLMUnavailable(f"Ollama is not reachable at {self.url}: open the app") from err
        if resp.status_code == 404:
            raise LLMUnavailable(f"model {self.model!r} not found: run `ollama pull {self.model}`")
        resp.raise_for_status()
        data = resp.json()
        return LLMResponse(
            content=data["message"]["content"],
            model=self.model,
            latency_s=round(time.perf_counter() - t0, 3),
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
            cost_usd=0.0,
        )
