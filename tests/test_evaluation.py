"""Stage 5: XBRL reference values, scoring rules, gold sampling and an end-to-end evaluation."""

import csv
from datetime import date

import pytest

from evaluation import evaluate, gold
from evaluation.metrics import macro_f1, numbers_match, score_labels, score_numeric
from evaluation.xbrl import parse_value, read_xbrl, reference_values
from extraction import extract
from ingestion.manifest import Manifest
from ingestion.storage import LocalStore
from parsing import parse
from tests.sample_10k import build_10k
from tests.test_extract import FakeLLM
from tests.test_parse_pipeline import _ingest

FY_END = date(2024, 9, 28)


def _ctx(cid: str, start: str | None, end: str, segment: bool = False) -> str:
    seg = (
        "<xbrli:segment><xbrldi:explicitMember dimension='dei:LegalEntityAxis'>sub:PowerCoMember"
        "</xbrldi:explicitMember></xbrli:segment>"
        if segment
        else ""
    )
    period = (
        f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>"
        if start
        else f"<xbrli:instant>{end}</xbrli:instant>"
    )
    return (
        f"<xbrli:context id='{cid}'><xbrli:entity><xbrli:identifier scheme='x'>1</xbrli:identifier>"
        f"{seg}</xbrli:entity><xbrli:period>{period}</xbrli:period></xbrli:context>"
    )


def _fact(name: str, ctx: str, text: str, **attrs) -> str:
    extra = " ".join(f"{k}='{v}'" for k, v in attrs.items())
    tag = f"ix:nonFraction name='{name}' contextRef='{ctx}' unitRef='usd' {extra}"
    return f"<{tag}>{text}</ix:nonFraction>"


# Prior year, a subsidiary and a quarter all come before the right fact, as in real filings.
IXBRL = (
    "<div style='display:none'><ix:header><ix:resources>"
    + _ctx("fy23", "2022-09-25", "2023-09-30")
    + _ctx("fy24", "2023-10-01", "2024-09-28")
    + _ctx("fy24_sub", "2023-10-01", "2024-09-28", segment=True)
    + _ctx("q4", "2024-06-30", "2024-09-28")
    + _ctx("now", None, "2024-10-18")
    + "</ix:resources></ix:header></div><table>"
    + _fact("us-gaap:Revenues", "fy23", "383,285", scale="6")
    + _fact("us-gaap:Revenues", "fy24_sub", "1,900", scale="6")
    + _fact("us-gaap:Revenues", "q4", "94,930", scale="6")
    + _fact("us-gaap:Revenues", "fy24", "391,035", scale="6")
    + _fact("us-gaap:NetIncomeLoss", "fy24", "90,000", scale="6")
    + _fact("dei:EntityNumberOfEmployees", "now", "164,000", unitRef="pure")
    + "</table>"
).encode()


# ---------- XBRL ----------


def test_reference_is_the_registrants_annual_value():
    ref = reference_values(b"<html><body>" + IXBRL + b"</body></html>", FY_END)
    assert (ref.revenue_usd, ref.revenue_concept) == (391_035_000_000, "us-gaap:Revenues")
    assert ref.net_income_usd == 90_000_000_000
    assert ref.employee_count == 164_000


def test_concept_priority_falls_back_to_contract_revenue():
    body = IXBRL.replace(
        b"us-gaap:Revenues", b"us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    )
    ref = reference_values(body, FY_END)
    assert ref.revenue_usd == 391_035_000_000
    assert ref.revenue_concept.endswith("ExcludingAssessedTax")


def test_no_annual_context_means_no_reference():
    ref = reference_values(IXBRL, date(2025, 9, 27))
    assert (ref.revenue_usd, ref.net_income_usd) == (None, None)


@pytest.mark.parametrize(
    "text, fmt, scale, sign, expected",
    [
        ("391,035", "ixt:num-dot-decimal", "6", None, 391_035_000_000),
        ("11,831", None, "6", "-", -11_831_000_000),
        ("—", "ixt:fixed-zero", "6", None, 0),
        ("1.234,5", "ixt:num-comma-decimal", "3", None, 1_234_500),
        ("(2.5)", None, "9", "-", -2_500_000_000),
        ("three", "ixt-sec:numwordsen", None, None, None),
    ],
)
def test_parse_value(text, fmt, scale, sign, expected):
    value = parse_value(text, fmt, scale, sign)
    assert (None if value is None else int(value)) == expected


def test_nil_facts_are_skipped():
    contexts, facts = read_xbrl(
        b"<html><body>"
        + _fact("us-gaap:Revenues", "c", "", **{"xsi:nil": "true"}).encode()
        + b"</body></html>"
    )
    assert facts == []


# ---------- Scoring ----------


def test_numeric_scoring_counts_wrong_values_against_both_precision_and_recall():
    s = score_numeric([
        (100, 100),        # TP, exact
        (100.4, 100),      # TP within 0.5%, not exact
        (90, 100),         # wrong: FP and FN
        (None, 100),       # missed: FN
        (5, None),         # invented: FP
        (None, None),      # correctly absent: exact only
    ])  # fmt: skip
    assert (s.n, s.tp, s.fp, s.fn, s.exact) == (6, 2, 2, 2, 2)
    assert (s.precision, s.recall, s.f1) == (0.5, 0.5, 0.5)
    assert s.exact_match == pytest.approx(2 / 6)


def test_empty_scores_are_undefined_not_zero():
    s = score_numeric([(None, None)])
    assert (s.precision, s.recall, s.f1, s.exact_match) == (None, None, None, 1.0)


def test_numbers_match_tolerance():
    assert numbers_match(391_035_000_000, 391_000_000_000)
    assert not numbers_match(36_010, 344_582)
    assert numbers_match(None, None) and not numbers_match(0, None)


def test_label_scores_are_one_vs_rest():
    pairs = [("cyber", "cyber"), ("cyber", "legal"), ("legal", "legal"), ("macro", "macro")]
    per = score_labels(pairs)
    assert (per["cyber"].precision, per["cyber"].recall) == (0.5, 1.0)
    assert (per["legal"].precision, per["legal"].recall) == (1.0, 0.5)
    assert macro_f1(per) == pytest.approx((2 / 3 + 2 / 3 + 1) / 3)


# ---------- Gold sampling ----------


def _docs():
    out = []
    for s, sector in enumerate(["Energy", "Utilities"]):
        for t in range(5):
            for y in (2023, 2024, 2025):
                out.append({"sha256": f"{s}{t}{y}", "accession_number": f"{s}{t}-{y}",
                            "ticker": f"{sector[0]}{t}", "sector": sector,
                            "period_end": date(y, 12, 31), "raw_uri": "x"})  # fmt: skip
    return out


def test_sample_is_three_companies_per_sector_and_reproducible():
    picked = gold.sample(_docs())
    assert len(picked) == 6
    assert len({d["ticker"] for d in picked}) == 6
    assert sorted({d["sector"] for d in picked}) == ["Energy", "Utilities"]
    assert picked == gold.sample(_docs())
    assert picked != gold.sample(_docs(), seed=1)


# ---------- End to end ----------


@pytest.fixture
def env(tmp_path):
    manifest = Manifest(":memory:")
    store = LocalStore(tmp_path / "raw")
    body = build_10k().replace(b"</body>", IXBRL + b"</body>")
    _ingest(manifest, store, "AAA", "0000000001-24-000001", body)
    manifest.con.execute(
        "UPDATE meta.documents SET report_date = DATE '2024-09-28', sector = 'Energy'"
    )
    parse.run(manifest.con, store)
    extract.run(manifest.con, FakeLLM())  # revenue 391,035M, net income 93,736M, 164,000
    yield manifest.con, store, tmp_path / "gold"
    manifest.close()


def _write(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def test_gold_files_prefill_from_xbrl_and_hide_the_models_category(env):
    con, store, _ = env
    picked = gold.sample(gold.documents(con))
    refs = gold.xbrl_references(con, store, picked)
    (row,) = gold.gold_rows(con, picked, refs)
    assert (row["revenue_usd"], row["net_income_usd"], row["employee_count"]) == (
        391_035_000_000, 90_000_000_000, 164_000)  # fmt: skip
    assert row["revenue_source"] == "xbrl:us-gaap:Revenues" and row["verified"] == ""
    assert "164,000" in row["employee_evidence"]
    (risk,) = gold.risk_rows(con, picked, None)
    assert risk["title"] == "Our business depends on global supply chains."
    assert "category" not in "".join(k for k in risk if k != "gold_category")


def test_evaluate_scores_against_xbrl_and_verified_gold(env):
    con, store, gold_dir = env
    acc = "0000000001-24-000001"
    _write(gold_dir / "gold_set.csv", gold.GOLD_COLUMNS, [
        {"accession_number": acc, "revenue_usd": "391035000000", "net_income_usd": "93736000000",
         "employee_count": "", "verified": "y"},  # a person found no employee count
        {"accession_number": "unchecked", "revenue_usd": "1", "verified": ""},
    ])  # fmt: skip
    _write(gold_dir / "risk_labels.csv", gold.RISK_COLUMNS, [
        {"accession_number": acc, "title": "Our business depends on global supply chains.",
         "gold_category": "geopolitical"},
    ])  # fmt: skip

    out = evaluate.run(con, store, gold_dir=gold_dir)
    res = out["models"]["fake-model"]
    s = res["scores"]
    assert out["gold_rows"] == 1  # the unverified row is ignored
    assert s[("xbrl", "revenue_usd")].tp == 1
    assert (s[("xbrl", "net_income_usd")].fp, s[("xbrl", "net_income_usd")].fn) == (1, 1)
    assert s[("gold", "net_income_usd")].tp == 1  # the person overrode the XBRL value
    assert (s[("gold", "employee_count")].fp, s[("gold", "employee_count")].tp) == (1, 0)
    assert s[("gold", "risk_category")].exact_match == 0.0
    stored = con.execute(
        "SELECT count(*) FROM eval.field_scores WHERE run_id = ?", [out["run_id"]]
    ).fetchone()[0]
    assert stored == len(s) + 1  # plus the macro F1 row
    fields = {r[0] for r in con.execute("SELECT field FROM eval.mismatches").fetchall()}
    assert fields == {"net_income_usd", "employee_count", "risk_category"}


def test_quarantined_filing_counts_as_a_miss(env):
    con, store, gold_dir = env
    con.execute("DELETE FROM extracted.filings")
    s = evaluate.run(con, store, models=["fake-model"], gold_dir=gold_dir)["models"]["fake-model"]
    assert (s["scores"][("xbrl", "revenue_usd")].fn, s["scores"][("xbrl", "revenue_usd")].tp) == (
        1,
        0,
    )
