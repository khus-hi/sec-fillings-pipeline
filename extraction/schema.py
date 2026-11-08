"""Stage 3: the extraction schema. What the LLM must return, and what we store.

Two layers:

* **What the model fills in** (`FinancialsExtraction`, `EmployeesExtraction`,
  `RiskFactorsExtraction`): only facts that have to be read from the text. Each one is
  small, so it fits a local 8B model's context together with the relevant chunks, and
  each comes with a JSON schema (`llm_json_schema`) the model is constrained to.
* **What we store** (`FilingRecord`): those answers plus everything we already know
  for certain from the Stage 1 manifest (CIK, ticker, company, fiscal period). The model
  is never asked for a fact we already have, so it can't get one wrong.

Money is extracted as (value, unit) exactly as printed ("391,035" + "millions"), and
converted to dollars in code. Asking a small model to multiply by a million is how
you get revenue that is off by 1,000x.

    python -m extraction.schema     # print the JSON schemas the model will be given
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "2"  # 2: quote fields required

# ---------- Risk categories ----------


class RiskCategory(StrEnum):
    MARKET_COMPETITION = "market_competition"
    MACROECONOMIC = "macroeconomic"
    REGULATORY_LEGAL = "regulatory_legal"
    OPERATIONAL_SUPPLY_CHAIN = "operational_supply_chain"
    CYBERSECURITY_TECHNOLOGY = "cybersecurity_technology"
    FINANCIAL_LIQUIDITY_CREDIT = "financial_liquidity_credit"
    GEOPOLITICAL = "geopolitical"
    CLIMATE_ENVIRONMENTAL = "climate_environmental"
    HUMAN_CAPITAL = "human_capital"
    STRATEGIC_REPUTATIONAL = "strategic_reputational"
    OTHER = "other"


# Shown to the model in the prompt (Stage 4) and used by people labelling the gold set
# (Stage 5), so both sides apply the same definitions.
_C = RiskCategory
CATEGORY_DESCRIPTIONS: dict[RiskCategory, str] = {
    _C.MARKET_COMPETITION: "competitors, pricing pressure, demand, customer concentration",
    _C.MACROECONOMIC: "recession, inflation, interest rates, currency exchange rates",
    _C.REGULATORY_LEGAL: "laws, regulation, litigation, tax, antitrust, compliance",
    _C.OPERATIONAL_SUPPLY_CHAIN: "suppliers, manufacturing, logistics, outages, quality",
    _C.CYBERSECURITY_TECHNOLOGY: "cyberattacks, data privacy, IT failures, AI, tech change",
    _C.FINANCIAL_LIQUIDITY_CREDIT: "debt, liquidity, credit ratings, capital, impairments",
    _C.GEOPOLITICAL: "war, sanctions, trade policy, tariffs, political instability",
    _C.CLIMATE_ENVIRONMENTAL: "climate change, weather, emissions rules, energy transition",
    _C.HUMAN_CAPITAL: "hiring and keeping talent, key people, labor relations, unions",
    _C.STRATEGIC_REPUTATIONAL: "M&A, strategy execution, brand, reputation, ESG",
    _C.OTHER: "anything that fits none of the above",
}

# Near-miss labels small models produce. Anything not listed still fails validation, so a
# retry can correct it rather than everything quietly becoming "other".
_CATEGORY_ALIASES = {
    "competition": RiskCategory.MARKET_COMPETITION,
    "market": RiskCategory.MARKET_COMPETITION,
    "economic": RiskCategory.MACROECONOMIC,
    "macro": RiskCategory.MACROECONOMIC,
    "regulatory": RiskCategory.REGULATORY_LEGAL,
    "legal": RiskCategory.REGULATORY_LEGAL,
    "operational": RiskCategory.OPERATIONAL_SUPPLY_CHAIN,
    "supply_chain": RiskCategory.OPERATIONAL_SUPPLY_CHAIN,
    "cybersecurity": RiskCategory.CYBERSECURITY_TECHNOLOGY,
    "cyber": RiskCategory.CYBERSECURITY_TECHNOLOGY,
    "technology": RiskCategory.CYBERSECURITY_TECHNOLOGY,
    "financial": RiskCategory.FINANCIAL_LIQUIDITY_CREDIT,
    "liquidity": RiskCategory.FINANCIAL_LIQUIDITY_CREDIT,
    "credit": RiskCategory.FINANCIAL_LIQUIDITY_CREDIT,
    "climate": RiskCategory.CLIMATE_ENVIRONMENTAL,
    "environmental": RiskCategory.CLIMATE_ENVIRONMENTAL,
    "talent": RiskCategory.HUMAN_CAPITAL,
    "labor": RiskCategory.HUMAN_CAPITAL,
    "strategic": RiskCategory.STRATEGIC_REPUTATIONAL,
    "reputational": RiskCategory.STRATEGIC_REPUTATIONAL,
}


def _normalize_category(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    key = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return _CATEGORY_ALIASES.get(key, key)


# ---------- Building blocks ----------

_Strict = ConfigDict(extra="forbid", str_strip_whitespace=True)
# Required (the key must be present) but nullable. An optional field is simply skipped by
# constrained decoding, which is what happened on the first real run: every quote was null.
Quote = Annotated[
    str | None,
    Field(
        max_length=500,
        description="The sentence or table row the value was read from, copied verbatim "
        "from the excerpt. null only when the value itself is null.",
    ),
]


class Unit(StrEnum):
    UNITS = "units"
    THOUSANDS = "thousands"
    MILLIONS = "millions"
    BILLIONS = "billions"


SCALE = {Unit.UNITS: 1, Unit.THOUSANDS: 10**3, Unit.MILLIONS: 10**6, Unit.BILLIONS: 10**9}


class ReportedAmount(BaseModel):
    """A money amount exactly as printed in the filing."""

    model_config = _Strict

    value: float = Field(
        description="The number as printed, without commas or currency symbols. "
        "Negative for a loss, e.g. (1,234) is -1234."
    )
    unit: Unit = Field(
        description='The scale stated for the table or sentence, e.g. "in millions".'
    )
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$", description="ISO 4217 code.")
    quote: Quote

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v

    def in_usd(self) -> int | None:
        """Whole dollars, or None if the amount isn't in USD."""
        if self.currency != "USD":
            return None
        return round(self.value * SCALE[self.unit])


# ---------- What the model fills in (one schema per LLM task) ----------


class FinancialsExtraction(BaseModel):
    """From the income statement (Item 8) or MD&A (Item 7), for the latest fiscal year."""

    model_config = _Strict

    revenue: ReportedAmount | None = Field(
        description="Total revenue for the most recent fiscal year (e.g. 'Total net sales', "
        "'Total revenues', 'Total net revenue' for banks). null if not in the text."
    )
    net_income: ReportedAmount | None = Field(
        description="Net income attributable to the company for the most recent fiscal year. "
        "Negative for a net loss. null if not in the text."
    )

    @field_validator("revenue")
    @classmethod
    def _revenue_positive(cls, v: ReportedAmount | None) -> ReportedAmount | None:
        if v is not None and v.value <= 0:
            raise ValueError("revenue must be positive")
        return v

    @model_validator(mode="after")
    def _distinct_lines(self) -> FinancialsExtraction:
        # Seen on the first full run (XOM): the same figure returned for both fields. The
        # error goes back to the model, which then gets a second chance at the right rows.
        rev, ni = self.revenue, self.net_income
        if rev and ni and rev.in_usd() is not None and rev.in_usd() == ni.in_usd():
            raise ValueError(
                "revenue and net_income are the same amount: revenue is the top line "
                "(total revenues or sales), net income is the bottom line"
            )
        return self


class EmployeesExtraction(BaseModel):
    """From Item 1, usually under "Human Capital"."""

    model_config = _Strict

    employee_count: int | None = Field(
        ge=1,
        le=5_000_000,
        description="Total number of employees (full-time or full-time equivalent) at fiscal "
        "year end. 'approximately 164,000' is 164000. null if not stated.",
    )
    quote: Quote


Category = Annotated[RiskCategory, BeforeValidator(_normalize_category)]


class RiskFactor(BaseModel):
    model_config = _Strict

    title: str = Field(
        min_length=10,
        max_length=300,
        description="The risk factor's heading or a one-sentence summary of it.",
    )
    category: Category


class RiskFactorsExtraction(BaseModel):
    """From Item 1A: the most significant risks, in the order the filing gives them."""

    model_config = _Strict

    risk_factors: list[RiskFactor] = Field(
        min_length=1,
        max_length=8,
        description="Up to 8 of the most significant risk factors, each with one category.",
    )

    @field_validator("risk_factors")
    @classmethod
    def _dedupe(cls, risks: list[RiskFactor]) -> list[RiskFactor]:
        seen, unique = set(), []
        for risk in risks:
            key = re.sub(r"\W+", " ", risk.title.lower()).strip()
            if key not in seen:
                seen.add(key)
                unique.append(risk)
        return unique


LLM_TASKS: dict[str, type[BaseModel]] = {
    "financials": FinancialsExtraction,
    "employees": EmployeesExtraction,
    "risk_factors": RiskFactorsExtraction,
}


# ---------- What we store ----------


def pad_cik(cik: str | int) -> str:
    digits = str(cik).strip()
    if not digits.isdigit() or len(digits) > 10:
        raise ValueError(f"not a CIK: {cik!r}")
    return digits.zfill(10)


def fiscal_year(period_end: date) -> int:
    """Companies name a fiscal year after the calendar year it ends in (Apple's year ending
    2024-09-28 is fiscal 2024, Walmart's ending 2025-01-31 is fiscal 2025)."""
    return period_end.year


class FilingRecord(BaseModel):
    """One validated extraction per filing: manifest facts + model answers + provenance."""

    model_config = ConfigDict(extra="forbid")

    # Known for certain from the Stage 1 manifest.
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cik: Annotated[str, BeforeValidator(pad_cik)]
    ticker: str = Field(pattern=r"^[A-Z.\-]{1,10}$")
    company_name: str | None = None
    period_end: date
    # Read from the text by the model.
    financials: FinancialsExtraction
    employees: EmployeesExtraction
    risks: RiskFactorsExtraction
    # Provenance.
    model: str
    schema_version: str = SCHEMA_VERSION
    parser_version: str
    extracted_at: datetime

    @computed_field
    @property
    def fiscal_year(self) -> int:
        return fiscal_year(self.period_end)

    @computed_field
    @property
    def revenue_usd(self) -> int | None:
        rev = self.financials.revenue
        return rev.in_usd() if rev else None

    @computed_field
    @property
    def net_income_usd(self) -> int | None:
        ni = self.financials.net_income
        return ni.in_usd() if ni else None

    @model_validator(mode="after")
    def _period_end_plausible(self) -> FilingRecord:
        if not 1993 <= self.period_end.year <= date.today().year + 1:
            raise ValueError(f"period_end {self.period_end} is outside EDGAR's range")
        return self


# Every company in ingestion/companies.csv is a large cap with well over 1,000 staff, so a
# smaller number almost always means "164" read from "164 thousand". Revisit for small caps.
MIN_PLAUSIBLE_EMPLOYEES = 1_000
# The highest net margins in this universe are around 55% (Visa, NVIDIA). Above 70%, the
# "revenue" is almost certainly a subtotal or the same row as net income.
MAX_NET_MARGIN = 0.7


def sanity_warnings(record: FilingRecord) -> list[str]:
    """Checks that a valid record can still fail: plausible-looking but probably wrong.
    Warnings don't reject a record; Stage 4 stores them so they can be reviewed."""
    warnings = []
    rev, ni = record.revenue_usd, record.net_income_usd
    if rev is not None and rev < 10**6:
        warnings.append(f"revenue ${rev:,} is under $1M: unit probably wrong")
    if rev is not None and rev > 2 * 10**12:
        warnings.append(f"revenue ${rev:,} is over $2T: unit probably wrong")
    if rev and ni is not None and abs(ni) > MAX_NET_MARGIN * rev:
        warnings.append(
            f"net income is {abs(ni) / rev:.0%} of revenue: probably the wrong line item or unit"
        )
    if record.financials.revenue and record.financials.revenue.currency != "USD":
        warnings.append(f"revenue reported in {record.financials.revenue.currency}, not USD")
    count = record.employees.employee_count
    if count is not None and count < MIN_PLAUSIBLE_EMPLOYEES:
        warnings.append(f"employee count {count} is under 1,000: probably in thousands")
    return warnings


# ---------- JSON schema for the model ----------


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:  # keep keys next to the $ref, e.g. the field's description
            target = defs[node["$ref"].rsplit("/", 1)[-1]]
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            return _inline_refs({**target, **siblings}, defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def llm_json_schema(task: str) -> dict[str, Any]:
    """The JSON schema for one LLM task, with every $ref inlined. Constrained decoding
    (Ollama's `format=`) handles a flat schema most reliably."""
    schema = LLM_TASKS[task].model_json_schema()
    return _inline_refs(schema, schema.get("$defs", {}))


def main() -> None:
    for task in LLM_TASKS:
        print(f"== {task}")
        print(json.dumps(llm_json_schema(task), indent=2))


if __name__ == "__main__":
    main()
