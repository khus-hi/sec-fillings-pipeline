"""Pick the text each LLM task sees. A small model with an 8k context can't read a 200,000
character section, and the first real test showed that the obvious shortcut (the first
chunk that mentions a keyword) hands it the wrong paragraph. Each task gets its own rule:

* employees     lines in Item 1 that pair an employee word with a number, plus neighbours
* financials    the Item 8 chunk (Item 7 as fallback) that looks most like an income statement
* risk_factors  the risk factor *headings* from the whole of Item 1A, in filing order

Everything is capped at MAX_CONTEXT_CHARS (~1,500 tokens) to leave room for the prompt
and the answer.
"""

from __future__ import annotations

import re

MAX_CONTEXT_CHARS = 6_000

_EMPLOYEE_WORD = re.compile(r"\b(employ(?:ees|ed)|headcount|workforce|full-time)\b", re.I)
_COUNT = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+(?:\.\d+)?\s*(?:thousand|million)\b", re.I)
_HUMAN_CAPITAL = re.compile(r"human capital", re.I)


def _cap(parts: list[str], limit: int = MAX_CONTEXT_CHARS) -> str:
    out, size = [], 0
    for part in parts:
        if size + len(part) + 1 > limit:
            remaining = limit - size - 1
            if not out and remaining > 0:  # a single huge part: keep its start
                out.append(part[:remaining])
            break
        out.append(part)
        size += len(part) + 1
    return "\n".join(out)


def employees_context(item_1: str) -> str:
    lines = item_1.split("\n")
    strong = [i for i, ln in enumerate(lines) if _EMPLOYEE_WORD.search(ln) and _COUNT.search(ln)]
    weak = [i for i, ln in enumerate(lines) if _HUMAN_CAPITAL.search(ln)]
    picked: list[int] = []
    # A count line plus its neighbours ("as of <date>", the unit); a "Human Capital"
    # heading plus the paragraph under it.
    windows = [(i - 1, i, i + 1) for i in strong] + [(i, i + 1) for i in weak]
    for window in windows:
        for j in window:
            if 0 <= j < len(lines) and j not in picked and lines[j].strip():
                picked.append(j)
    if not picked:
        return _cap([item_1])
    return _cap([lines[j] for j in picked])  # strongest evidence first, each in reading order


_FIN_SIGNALS = [
    (re.compile(r"\bnet income\b|\bnet (?:loss|earnings)\b", re.I), 3),
    (
        re.compile(r"\bnet sales\b|total (?:net )?revenues?|\bnet revenues?\b|\brevenues?\b", re.I),
        3,
    ),
    (re.compile(r"\(?in (?:millions|thousands|billions)\b", re.I), 2),
    (re.compile(r"consolidated statements? of (?:operations|income|earnings)", re.I), 2),
    (re.compile(r"per share", re.I), 1),
]


def score_financial_chunk(text: str) -> int:
    return sum(weight for regex, weight in _FIN_SIGNALS if regex.search(text))


def financials_context(item_8_chunks: list[str], item_7_chunks: list[str]) -> str:
    """Highest-scoring chunk; earliest wins a tie (the income statement comes first)."""
    for chunks in (item_8_chunks, item_7_chunks):
        scored = [(score_financial_chunk(c), -i, c) for i, c in enumerate(chunks)]
        if scored:
            best = max(scored)
            if best[0] >= 6:  # at least net income + revenue
                return _cap([best[2]])
    fallback = item_8_chunks or item_7_chunks
    return _cap([fallback[0]]) if fallback else ""


def risk_headings(item_1a: str) -> list[str]:
    """Risk factor headings: short-to-medium lines followed by a longer body paragraph.
    Filers set each risk in bold or italics on its own line, so after HTML-to-text it is a
    line of its own; the explanation under it is a much longer line."""
    lines = [ln.strip() for ln in item_1a.split("\n") if ln.strip()]
    if len(lines) < 2:
        return []
    headings = []
    for line, nxt in zip(lines[1:], lines[2:] + [""], strict=True):  # lines[0] = "Item 1A..."
        if (
            15 <= len(line) <= 400
            and " | " not in line
            and not line[0].islower()
            and len(nxt) > max(len(line), 200)
        ):
            headings.append(line)
    return headings


def risk_context(item_1a: str) -> str:
    headings = risk_headings(item_1a)
    if len(headings) < 3:  # no recognisable headings: fall back to the opening text
        return _cap([item_1a])
    return _cap(headings)
