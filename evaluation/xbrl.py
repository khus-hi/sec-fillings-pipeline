"""Reference values from a filing's own inline XBRL tags.

Since 2019 every 10-K is inline XBRL: the numbers in the financial statements are wrapped in
<ix:nonFraction> tags that name a us-gaap concept and a context (which entity, which period).
That gives a reference answer for revenue and net income that does not depend on the LLM, the
parser or a human reading tables. It is used to pre-fill the gold set and to score every filing.

Pure functions only: raw bytes in, values out. No network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from lxml import etree

XBRL_VERSION = "2"  # 2: revenue = the largest registrant revenue fact (top line)

# Revenue is the top line, so the reference is the largest of these for the registrant's year:
# a component (contract revenue only, one line of business tagged "Revenues") is never larger
# than the total. First tried version 1, "first concept found wins"; it picked AXP's 37.2B
# component over its 60.5B total, and a narrower contract-revenue tag for DUK and NEE.
REVENUE_CONCEPTS = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:SalesRevenueNet",
    "us-gaap:RegulatedAndUnregulatedOperatingRevenue",
    "us-gaap:RevenuesNetOfInterestExpense",
)
# Banks report revenue net of interest expense; their gross figure is not the top line.
BANK_REVENUE_CONCEPT = "us-gaap:RevenuesNetOfInterestExpense"
# Net income attributable to the parent first, as the prompt asks for.
NET_INCOME_CONCEPTS = (
    "us-gaap:NetIncomeLoss",
    "us-gaap:ProfitLoss",
    "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
)
EMPLOYEE_CONCEPTS = ("dei:EntityNumberOfEmployees",)

_ZERO_TEXT = {"-", "—", "–", "nil", "none"}


@dataclass(frozen=True)
class Context:
    id: str
    start: date | None
    end: date | None  # end date, or the instant
    has_dimensions: bool  # a segment means a subsidiary, a business line, a region...


@dataclass(frozen=True)
class Fact:
    concept: str
    context_id: str
    value: Decimal


@dataclass(frozen=True)
class Reference:
    revenue_usd: int | None
    revenue_concept: str | None
    net_income_usd: int | None
    net_income_concept: str | None
    employee_count: int | None


def _local(tag) -> str:
    """'xbrli:startDate', '{ns}startDate' and 'xbrli:startdate' all become 'startdate'."""
    if not isinstance(tag, str):
        return ""  # comments, processing instructions
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _attr(el, name: str) -> str | None:
    for key, value in el.attrib.items():
        if _local(key) == name:
            return value
    return None


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def parse_value(text: str, fmt: str | None, scale: str | None, sign: str | None) -> Decimal | None:
    """The number an <ix:nonFraction> stands for: displayed text, format, scale and sign."""
    fmt = (fmt or "").lower()
    raw = text.strip().strip("()$").strip()
    if "zero" in fmt or "dash" in fmt or raw.lower() in _ZERO_TEXT:
        number = Decimal(0)
    else:
        if "commadecimal" in fmt.replace("-", ""):  # 1.234,5
            raw = raw.replace(".", "").replace(" ", "").replace(",", ".")
        else:
            raw = raw.replace(",", "").replace(" ", "")
        try:
            number = Decimal(raw)
        except InvalidOperation:
            return None  # e.g. numbers written as words
    number = number.scaleb(int(scale or 0))
    return -number if sign == "-" else number


def read_xbrl(raw: bytes) -> tuple[dict[str, Context], list[Fact]]:
    root = etree.fromstring(raw, etree.HTMLParser(huge_tree=True))
    contexts: dict[str, Context] = {}
    facts: list[Fact] = []
    if root is None:
        return contexts, facts
    for el in root.iter():
        name = _local(el.tag)
        if name == "context":
            cid = _attr(el, "id")
            dates = {_local(c.tag): _date(c.text) for c in el.iter()}
            has_dims = any(_local(c.tag) in ("segment", "scenario") for c in el.iter())
            if cid:
                contexts[cid] = Context(
                    cid,
                    dates.get("startdate"),
                    dates.get("enddate") or dates.get("instant"),
                    has_dims,
                )
        elif name == "nonfraction":
            if (_attr(el, "nil") or "").lower() == "true":
                continue
            value = parse_value(
                "".join(el.itertext()), _attr(el, "format"), _attr(el, "scale"), _attr(el, "sign")
            )
            concept, cid = _attr(el, "name"), _attr(el, "contextref")
            if value is not None and concept and cid:
                facts.append(Fact(concept, cid, value))
    return contexts, facts


def _annual(ctx: Context, period_end: date) -> bool:
    if ctx.has_dimensions or ctx.start is None or ctx.end is None:
        return False
    one_year = 340 <= (ctx.end - ctx.start).days <= 380  # 52/53-week years included
    return one_year and abs(ctx.end - period_end) <= timedelta(days=3)


def _first(facts, contexts, concepts, period_end, annual: bool = True):
    for concept in concepts:
        for f in facts:
            ctx = contexts.get(f.context_id)
            if f.concept != concept or ctx is None:
                continue
            if annual and not _annual(ctx, period_end):
                continue
            if not annual and ctx.has_dimensions:
                continue
            return int(f.value), concept
    return None, None


def _registrant_annual(facts, contexts, concepts, period_end) -> list[Fact]:
    return [
        f for f in facts
        if f.concept in concepts
        and f.context_id in contexts
        and _annual(contexts[f.context_id], period_end)
    ]  # fmt: skip


def top_line_revenue(facts, contexts, period_end: date) -> tuple[int | None, str | None]:
    candidates = _registrant_annual(facts, contexts, REVENUE_CONCEPTS, period_end)
    bank = [f for f in candidates if f.concept == BANK_REVENUE_CONCEPT]
    if bank or not candidates:
        return (int(bank[0].value), bank[0].concept) if bank else (None, None)
    best = max(candidates, key=lambda f: f.value)
    return int(best.value), best.concept


def reference_values(raw: bytes, period_end: date) -> Reference:
    """Revenue and net income for the fiscal year ending `period_end`, for the registrant
    itself (no dimensions, so not a subsidiary or segment), plus the employee count when the
    company tags it."""
    contexts, facts = read_xbrl(raw)
    revenue, revenue_concept = top_line_revenue(facts, contexts, period_end)
    net_income, net_income_concept = _first(facts, contexts, NET_INCOME_CONCEPTS, period_end)
    employees, _ = _first(facts, contexts, EMPLOYEE_CONCEPTS, period_end, annual=False)
    return Reference(revenue, revenue_concept, net_income, net_income_concept, employees)
