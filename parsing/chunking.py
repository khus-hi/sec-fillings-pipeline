"""Split a section into overlapping chunks small enough for one LLM call.

Why chunk at all: Item 1A alone can be 100k+ characters, far beyond what an 8B model
running locally handles well in one prompt (we budget about 8k tokens of context for
prompt + chunk + JSON answer).

How:
  - Each line from html_text is one block (a paragraph, heading or table row). Pack
    whole blocks into a chunk until it reaches `max_chars`, so a risk factor or a
    table row is never cut in half when it can be avoided.
  - Carry the last block(s), up to `overlap_chars`, into the next chunk, so a
    fact that straddles a boundary appears whole in at least one chunk.
  - A single block longer than `max_chars` is split on sentences, then hard-cut as a
    last resort.

Sizes are in characters, not tokens. English text averages about 4 characters per
token, so 6,000 chars is roughly 1,500 tokens. Counting characters needs no tokenizer
dependency and is identical on every machine; `approx_tokens` is reported for logging.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_MAX_CHARS = 6_000
DEFAULT_OVERLAP_CHARS = 600
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str

    @property
    def approx_tokens(self) -> int:
        return max(1, len(self.text) // CHARS_PER_TOKEN)


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """Split one oversized paragraph into pieces of at most max_chars."""
    pieces = [p for p in re.split(r"(?<=[.!?])\s+", paragraph) if p.strip()]
    if len(pieces) > 1:
        out: list[str] = []
        for p in pieces:
            out.extend(_split_long(p, max_chars) if len(p) > max_chars else [p])
        return _pack(out, max_chars, sep=" ")
    return [paragraph[i : i + max_chars] for i in range(0, len(paragraph), max_chars)]


def _pack(pieces: list[str], max_chars: int, sep: str) -> list[str]:
    out: list[str] = []
    current = ""
    for p in pieces:
        candidate = f"{current}{sep}{p}" if current else p
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                out.append(current)
            current = p
    if current:
        out.append(current)
    return out


def chunk_text(
    text: str, max_chars: int = DEFAULT_MAX_CHARS, overlap_chars: int = DEFAULT_OVERLAP_CHARS
) -> list[Chunk]:
    if overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be smaller than max_chars")

    paragraphs: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        paragraphs.extend(_split_long(para, max_chars) if len(para) > max_chars else [para])

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for para in paragraphs:
        added = len(para) + (1 if current else 0)
        if current and size + added > max_chars:
            chunks.append("\n".join(current))
            # Start the next chunk with trailing paragraphs that fit in the overlap.
            carry: list[str] = []
            carry_size = 0
            for prev in reversed(current):
                if carry_size + len(prev) + 1 > overlap_chars:
                    break
                carry.insert(0, prev)
                carry_size += len(prev) + 1
            # Never let the overlap push the new paragraph over the limit.
            while carry and carry_size + len(para) + 1 > max_chars:
                carry_size -= len(carry.pop(0)) + 1
            current, size = carry, carry_size
            added = len(para) + (1 if current else 0)
        current.append(para)
        size += added
    if current:
        chunks.append("\n".join(current))
    return [Chunk(i, c) for i, c in enumerate(chunks)]
