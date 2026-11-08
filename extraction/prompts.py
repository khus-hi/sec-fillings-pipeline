"""Prompts for each LLM task. PROMPT_VERSION is part of the extraction cache key: change a
prompt, bump it, and the next run re-extracts."""

from __future__ import annotations

from extraction.schema import CATEGORY_DESCRIPTIONS

PROMPT_VERSION = "1"

SYSTEM = (
    "You extract facts from SEC 10-K filings into JSON. Use only the excerpt you are given. "
    "If a value is not in the excerpt, use null; never guess. Copy numbers exactly as "
    "printed and copy the quote fields verbatim from the excerpt."
)

_CATEGORIES = "\n".join(f"- {c.value}: {d}" for c, d in CATEGORY_DESCRIPTIONS.items())

_TASK = {
    "financials": (
        "From this excerpt of {company}'s 10-K (fiscal year ending {period_end}), extract total "
        "revenue and net income for the fiscal year ending {period_end} (the most recent "
        "column). Give each number as printed in the table, the unit stated in the table "
        "header (e.g. 'in millions'), and the table row it came from as the quote."
    ),
    "employees": (
        "From this excerpt of {company}'s 10-K, extract the total number of employees. Use the "
        "company-wide total, not a single segment or country, and quote the sentence."
    ),
    "risk_factors": (
        "These are the risk factor headings from Item 1A of {company}'s 10-K, in filing order. "
        "Choose up to 8 of the most significant, distinct risks. For each, give a title (the "
        "heading, shortened to one sentence if long) and exactly one category:\n" + _CATEGORIES
    ),
}


def build_messages(task: str, context: str, company: str, period_end: str) -> list[dict]:
    instruction = _TASK[task].format(company=company or "the company", period_end=period_end)
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"{instruction}\n\nEXCERPT:\n{context}"},
    ]


def retry_message(errors: list[str]) -> dict:
    """Sent after an invalid answer, so the model can correct exactly what failed."""
    listed = "\n".join(f"- {e}" for e in errors[:10])
    return {
        "role": "user",
        "content": f"Your answer failed validation:\n{listed}\nReturn the corrected JSON only.",
    }
