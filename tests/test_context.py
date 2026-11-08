from extraction.context import (
    MAX_CONTEXT_CHARS,
    employees_context,
    financials_context,
    risk_context,
    risk_headings,
)

BODY = "This risk could hurt our results in many ways that we describe at length here. " * 6


def test_employees_context_finds_the_count_not_the_first_mention():
    item_1 = "\n".join(
        [
            "Item 1. Business",
            "Our employees are our greatest asset and we invest in them.",  # no number
            "We operate 4,200 branches in 48 states.",  # number, no employee word
            "Human Capital",
            "As of December 31, 2025, we had 317,233 employees worldwide.",
            "Unrelated paragraph about products.",
        ]
    )
    text = employees_context(item_1)
    assert text.startswith("Human Capital\nAs of December 31, 2025, we had 317,233 employees")
    assert "4,200 branches" not in text


def test_employees_context_falls_back_to_section_start():
    assert employees_context("Item 1. Business\nWe make things.") == (
        "Item 1. Business\nWe make things."
    )


def test_financials_context_prefers_income_statement_chunk():
    notes = "Note 5 | Leases | right-of-use assets | 1,234"
    statement = (
        "CONSOLIDATED STATEMENTS OF OPERATIONS\n(In millions, except per share amounts)\n"
        "Total net sales | 391,035 | 383,285\nNet income | $ | 93,736 | $ | 96,995"
    )
    auditor = "Report of Independent Registered Public Accounting Firm. Revenue recognition."
    assert financials_context([auditor, statement, notes], []) == statement


def test_financials_context_uses_mdna_when_item_8_has_no_statement():
    mdna = "Total revenues were $10 (in millions). Net income was $2 per share data."
    assert financials_context(["Index to financial statements | F-1"], [mdna]) == mdna


def test_risk_headings_are_lines_followed_by_long_bodies():
    item_1a = "\n".join(
        [
            "Item 1A. Risk Factors",
            "Macroeconomic and Industry Risks",
            "Adverse economic conditions could reduce demand for our products.",
            BODY,
            "We face intense competition in all of our markets.",
            BODY,
            "Year | 2025 | 2024",  # table row, not a heading
            BODY,
            "and continues mid-sentence after a page break, so it is not a heading",
            BODY,
        ]
    )
    assert risk_headings(item_1a) == [
        "Adverse economic conditions could reduce demand for our products.",
        "We face intense competition in all of our markets.",
    ]
    assert risk_context(item_1a).startswith("Item 1A")  # < 3 headings: fall back to text


def test_risk_context_lists_headings_from_the_whole_section_within_the_cap():
    item_1a = "Item 1A. Risk Factors\n" + "\n".join(
        f"Risk number {i} could materially harm our business.\n{BODY}" for i in range(400)
    )
    text = risk_context(item_1a)
    assert text.startswith("Risk number 0 could")
    assert BODY.strip() not in text
    assert len(text) <= MAX_CONTEXT_CHARS


def test_context_is_capped():
    huge = "x" * (MAX_CONTEXT_CHARS * 3)
    assert len(employees_context(huge)) <= MAX_CONTEXT_CHARS
