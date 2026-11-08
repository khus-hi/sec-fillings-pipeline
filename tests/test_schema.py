import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from extraction.schema import (
    CATEGORY_DESCRIPTIONS,
    LLM_TASKS,
    SCHEMA_VERSION,
    EmployeesExtraction,
    FilingRecord,
    FinancialsExtraction,
    ReportedAmount,
    RiskCategory,
    RiskFactorsExtraction,
    fiscal_year,
    llm_json_schema,
    sanity_warnings,
)

# What a model answer looks like, as raw JSON text.
FINANCIALS_JSON = """{
  "revenue": {"value": 391035, "unit": "millions", "quote": "Total net sales | $ | 391,035"},
  "net_income": {"value": 93736, "unit": "millions", "quote": "Net income | $ | 93,736"}
}"""


def _record(**overrides) -> FilingRecord:
    data = {
        "accession_number": "0000320193-24-000123",
        "sha256": "a" * 64,
        "cik": "320193",
        "ticker": "AAPL",
        "company_name": "Apple Inc.",
        "period_end": date(2024, 9, 28),
        "financials": FinancialsExtraction.model_validate_json(FINANCIALS_JSON),
        "employees": {"employee_count": 164000, "quote": "approximately 164,000"},
        "risks": {
            "risk_factors": [
                {"title": "Global economic conditions could hurt demand", "category": "macro"}
            ]
        },
        "model": "local-test-model",
        "parser_version": "2",
        "extracted_at": datetime(2026, 11, 8, tzinfo=UTC),
    }
    return FilingRecord.model_validate({**data, **overrides})


# ---------- Money ----------


def test_amount_is_converted_to_dollars_in_code():
    fin = FinancialsExtraction.model_validate_json(FINANCIALS_JSON)
    assert fin.revenue.in_usd() == 391_035_000_000
    assert fin.net_income.in_usd() == 93_736_000_000


def test_net_loss_may_be_negative_but_revenue_may_not():
    fin = FinancialsExtraction(
        revenue={"value": 10, "unit": "billions", "quote": "Revenue 10"},
        net_income={"value": -2.5, "unit": "billions", "quote": "Net loss (2.5)"},
    )
    assert fin.net_income.in_usd() == -2_500_000_000
    with pytest.raises(ValidationError, match="revenue must be positive"):
        FinancialsExtraction(
            revenue={"value": -1, "unit": "millions", "quote": "x"}, net_income=None
        )


def test_unknown_unit_and_extra_keys_are_rejected():
    with pytest.raises(ValidationError):
        ReportedAmount(value=1, unit="lakhs", quote=None)
    with pytest.raises(ValidationError, match="Extra inputs"):
        FinancialsExtraction.model_validate(
            {"revenue": None, "net_income": None, "confidence": 0.9}
        )


def test_non_usd_amount_has_no_dollar_value():
    assert ReportedAmount(value=5, unit="millions", currency="EUR", quote="x").in_usd() is None


def test_missing_values_must_be_explicit_null():
    # The model has to say "not found" rather than silently drop a key.
    with pytest.raises(ValidationError, match="net_income"):
        FinancialsExtraction.model_validate({"revenue": None})
    assert FinancialsExtraction(revenue=None, net_income=None).revenue is None


# ---------- Employees ----------


@pytest.mark.parametrize("bad", [0, -5, 10_000_000, "about 164k"])
def test_employee_count_bounds(bad):
    with pytest.raises(ValidationError):
        EmployeesExtraction(employee_count=bad)


def test_employee_count_from_json_string_number():
    parsed = EmployeesExtraction.model_validate_json('{"employee_count": "164000", "quote": null}')
    assert parsed.employee_count == 164000


def test_quote_key_is_required():
    with pytest.raises(ValidationError, match="quote"):
        EmployeesExtraction.model_validate_json('{"employee_count": 164000}')
    with pytest.raises(ValidationError, match="quote"):
        ReportedAmount.model_validate({"value": 1, "unit": "millions"})


# ---------- Risk factors ----------


def test_category_near_misses_are_normalised():
    risks = RiskFactorsExtraction(
        risk_factors=[
            {"title": "Supply disruptions in Asia", "category": "Supply Chain"},
            {"title": "Data breaches and ransomware", "category": "CYBER"},
            {"title": "Changes in tax law and audits", "category": "regulatory-legal"},
        ]
    )
    assert [r.category for r in risks.risk_factors] == [
        RiskCategory.OPERATIONAL_SUPPLY_CHAIN,
        RiskCategory.CYBERSECURITY_TECHNOLOGY,
        RiskCategory.REGULATORY_LEGAL,
    ]


def test_unknown_category_fails_instead_of_becoming_other():
    with pytest.raises(ValidationError, match="category"):
        RiskFactorsExtraction(
            risk_factors=[{"title": "Alien invasion of the supply chain", "category": "aliens"}]
        )


def test_risk_list_bounds_and_dedupe():
    risk = {"title": "Intense competition in all markets", "category": "market_competition"}
    with pytest.raises(ValidationError):
        RiskFactorsExtraction(risk_factors=[])
    with pytest.raises(ValidationError):
        RiskFactorsExtraction(risk_factors=[risk] * 9)
    dup = {**risk, "title": "Intense competition, in all markets."}
    assert len(RiskFactorsExtraction(risk_factors=[risk, dup]).risk_factors) == 1


def test_every_category_has_a_description():
    assert set(CATEGORY_DESCRIPTIONS) == set(RiskCategory)


# ---------- Stored record ----------


def test_record_derives_fiscal_year_dollars_and_padded_cik():
    rec = _record()
    assert rec.cik == "0000320193"
    assert (rec.fiscal_year, rec.revenue_usd, rec.net_income_usd) == (
        2024,
        391_035_000_000,
        93_736_000_000,
    )
    dumped = json.loads(rec.model_dump_json())
    assert dumped["revenue_usd"] == 391_035_000_000 and dumped["schema_version"] == SCHEMA_VERSION


@pytest.mark.parametrize("period_end, year", [(date(2024, 9, 28), 2024), (date(2025, 1, 31), 2025)])
def test_fiscal_year_is_the_year_the_period_ends(period_end, year):
    assert fiscal_year(period_end) == year


@pytest.mark.parametrize(
    "field, value",
    [
        ("cik", "CIK320193"),
        ("accession_number", "320193-24-123"),
        ("sha256", "not-a-hash"),
        ("period_end", date(1980, 1, 1)),
    ],
)
def test_record_rejects_bad_manifest_fields(field, value):
    with pytest.raises(ValidationError):
        _record(**{field: value})


def test_clean_record_has_no_warnings():
    assert sanity_warnings(_record()) == []


def test_sanity_warnings_catch_unit_mistakes():
    wrong_unit = _stored_before_margin_rule(
        ReportedAmount(value=391035, unit="units", quote="391,035"),
        ReportedAmount(value=93736, unit="millions", quote="93,736"),
    )
    record = _record(employees={"employee_count": 164, "quote": None})
    record.financials = wrong_unit  # assignment isn't re-validated, like an old stored row
    warnings = sanity_warnings(record)
    assert any("under $1M" in w for w in warnings)
    assert any("of revenue: probably the wrong line item" in w for w in warnings)
    assert any("employee count 164" in w for w in warnings)


# ---------- JSON schema handed to the model ----------


@pytest.mark.parametrize("task", list(LLM_TASKS))
def test_llm_schema_is_flat_json(task):
    schema = llm_json_schema(task)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False


def test_llm_schema_lists_allowed_values():
    risk = llm_json_schema("risk_factors")["properties"]["risk_factors"]["items"]
    assert set(risk["properties"]["category"]["enum"]) == {c.value for c in RiskCategory}
    fin = llm_json_schema("financials")["properties"]["revenue"]["anyOf"][0]
    assert fin["properties"]["unit"]["enum"] == ["units", "thousands", "millions", "billions"]
    assert "in millions" in fin["properties"]["unit"]["description"]  # survives inlining
    assert set(llm_json_schema("financials")["required"]) == {"revenue", "net_income"}


def test_same_amount_for_revenue_and_net_income_is_rejected():
    same = {"value": 36010, "unit": "millions", "quote": "36,010"}
    with pytest.raises(ValidationError, match="same amount"):
        FinancialsExtraction(revenue=same, net_income=same)


def test_implausible_net_margin_is_rejected_so_the_model_retries():
    with pytest.raises(ValidationError, match="80% of revenue"):
        FinancialsExtraction(
            revenue={"value": 100, "unit": "billions", "quote": "Total revenues 100"},
            net_income={"value": 80, "unit": "billions", "quote": "Net income 80"},
        )


def _stored_before_margin_rule(revenue, net_income) -> FinancialsExtraction:
    """A record extracted before the margin rule existed (validation skipped)."""
    return FinancialsExtraction.model_construct(revenue=revenue, net_income=net_income)


def test_sanity_warning_still_covers_records_stored_before_the_rule():
    fin = _stored_before_margin_rule(
        ReportedAmount(value=100, unit="billions", quote="Total revenues 100"),
        ReportedAmount(value=80, unit="billions", quote="Net income 80"),
    )
    record = _record()
    record.financials = fin
    assert sanity_warnings(record) == [
        "net income is 80% of revenue: probably the wrong line item or unit"
    ]
