"""Score the extracted records: python -m evaluation.evaluate

Two references, reported separately:

- gold: the hand-verified rows of evaluation/gold/gold_set.csv (revenue, net income, employee
  count) and the labelled rows of risk_labels.csv (risk category).
- xbrl: revenue and net income for every filing, against the filing's own inline XBRL tags.
  Automatic, so it covers all filings, but it trusts the company's choice of concept.

A filing with no record (quarantined) counts as a null prediction, so quarantine costs recall.
Every run is stored in eval.runs, eval.field_scores and eval.mismatches, per model.
"""

from __future__ import annotations

import argparse
import csv
import uuid

import duckdb

from evaluation.gold import GOLD_SET, RISK_LABELS, documents, ensure_schema, xbrl_references
from evaluation.metrics import (
    NUMERIC_TOLERANCE,
    FieldScore,
    macro_f1,
    numbers_match,
    score_labels,
    score_numeric,
)
from evaluation.xbrl import XBRL_VERSION
from extraction.schema import RiskCategory
from ingestion.config import get_settings, get_store
from ingestion.manifest import utcnow

NUMERIC_FIELDS = ("revenue_usd", "net_income_usd", "employee_count")

DDL = """
CREATE SCHEMA IF NOT EXISTS eval;
CREATE TABLE IF NOT EXISTS eval.runs (
    run_id            VARCHAR NOT NULL,
    model             VARCHAR NOT NULL,
    extractor_version VARCHAR,
    xbrl_version      VARCHAR NOT NULL,
    gold_rows         INTEGER,      -- verified rows in gold_set.csv
    risk_label_rows   INTEGER,      -- labelled and matched rows in risk_labels.csv
    tolerance         DOUBLE NOT NULL,
    evaluated_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (run_id, model)
);
CREATE TABLE IF NOT EXISTS eval.field_scores (
    run_id      VARCHAR NOT NULL,
    model       VARCHAR NOT NULL,
    reference   VARCHAR NOT NULL,   -- gold | xbrl
    field       VARCHAR NOT NULL,   -- revenue_usd ... or risk_category[:label]
    n           INTEGER, tp INTEGER, fp INTEGER, fn INTEGER, exact INTEGER,
    precision   DOUBLE, recall DOUBLE, f1 DOUBLE, exact_match DOUBLE,
    PRIMARY KEY (run_id, model, reference, field)
);
CREATE TABLE IF NOT EXISTS eval.mismatches (
    run_id           VARCHAR NOT NULL,
    model            VARCHAR NOT NULL,
    reference        VARCHAR NOT NULL,
    field            VARCHAR NOT NULL,
    accession_number VARCHAR NOT NULL,
    ticker           VARCHAR,
    fiscal_year      INTEGER,
    predicted        VARCHAR,
    expected         VARCHAR
);
"""


def _int(cell: str | None) -> int | None:
    cell = (cell or "").replace(",", "").strip()
    return int(float(cell)) if cell else None


def read_gold(path=None) -> list[dict]:
    """Verified rows only: an unchecked pre-fill is not a label."""
    path = path or GOLD_SET
    if not path.exists():
        return []
    with path.open(newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("verified", "").strip().lower() == "y"]
    return [
        {"accession_number": r["accession_number"], **{k: _int(r.get(k)) for k in NUMERIC_FIELDS}}
        for r in rows
    ]


def read_risk_labels(path=None) -> list[dict]:
    path = path or RISK_LABELS
    if not path.exists():
        return []
    with path.open(newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("gold_category", "").strip()]
    for r in rows:
        r["gold_category"] = RiskCategory(r["gold_category"].strip().lower()).value
    return rows


def _predictions(con, model: str) -> dict[str, dict]:
    """Every parsed document, with this model's record if there is one."""
    rows = con.execute(
        """SELECT d.accession_number, d.ticker, year(d.report_date), f.revenue_usd,
                  f.net_income_usd, f.employee_count, f.risk_factors
           FROM meta.documents d
           JOIN parsed.documents p ON p.sha256 = d.sha256 AND p.status = 'ok'
           LEFT JOIN extracted.filings f ON f.sha256 = d.sha256 AND f.model = ?""",
        [model],
    ).fetchall()
    keys = ["accession_number", "ticker", "fiscal_year", *NUMERIC_FIELDS, "risk_factors"]
    return {r[0]: dict(zip(keys, r, strict=True)) for r in rows}


def evaluate_model(con, model: str, refs_by_accession: dict, gold: list[dict], labels: list[dict]):
    """(scores, mismatches) for one model. scores: {(reference, field): FieldScore}."""
    preds = _predictions(con, model)
    scores: dict[tuple[str, str], FieldScore] = {}
    mismatches: list[tuple] = []

    def numeric(reference: str, field: str, expected: dict[str, int | None]) -> None:
        pairs = []
        for acc, exp in expected.items():
            pred = preds.get(acc, {}).get(field)
            pairs.append((pred, exp))
            if not numbers_match(pred, exp):
                p = preds.get(acc, {})
                mismatches.append((reference, field, acc, p.get("ticker"), p.get("fiscal_year"),
                                   str(pred), str(exp)))  # fmt: skip
        scores[(reference, field)] = score_numeric(pairs)

    for field in ("revenue_usd", "net_income_usd"):
        expected = {acc: getattr(ref, field) for acc, ref in refs_by_accession.items()
                    if getattr(ref, field) is not None}  # fmt: skip
        numeric("xbrl", field, expected)
    if gold:
        for field in NUMERIC_FIELDS:
            numeric("gold", field, {g["accession_number"]: g[field] for g in gold})

    pairs = []
    for row in labels:
        risks = preds.get(row["accession_number"], {}).get("risk_factors") or []
        predicted = next((r["category"] for r in risks if r["title"] == row["title"]), None)
        if predicted is None:
            continue  # titles changed since labelling; reported as unmatched
        pairs.append((predicted, row["gold_category"]))
        if predicted != row["gold_category"]:
            p = preds[row["accession_number"]]
            mismatches.append(("gold", "risk_category", row["accession_number"], p["ticker"],
                               p["fiscal_year"], f"{predicted}: {row['title']}",
                               row["gold_category"]))  # fmt: skip
    if pairs:
        per_label = score_labels(pairs)
        correct = sum(p == g for p, g in pairs)
        scores[("gold", "risk_category")] = FieldScore(len(pairs), correct, len(pairs) - correct,
                                                       len(pairs) - correct, correct)  # fmt: skip
        for label, s in per_label.items():
            scores[("gold", f"risk_category:{label}")] = s
    return scores, mismatches, len(pairs)


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def markdown_table(scores: dict[tuple[str, str], FieldScore], macro: float | None) -> str:
    lines = ["| Reference | Field | n | Precision | Recall | F1 | Exact match |",
             "|---|---|---:|---:|---:|---:|---:|"]  # fmt: skip
    for (reference, field), s in sorted(scores.items()):
        if ":" in field:
            continue
        lines.append(f"| {reference} | {field} | {s.n} | {_fmt(s.precision)} | "
                     f"{_fmt(s.recall)} | {_fmt(s.f1)} | {_fmt(s.exact_match)} |")  # fmt: skip
    if macro is not None:
        lines.append(f"| gold | risk_category (macro F1 over categories) | | | | {_fmt(macro)} | |")
    return "\n".join(lines)


def run(
    con: duckdb.DuckDBPyConnection, store, models: list[str] | None = None, gold_dir=None
) -> dict:
    ensure_schema(con)
    con.execute(DDL)
    docs = documents(con)
    refs = xbrl_references(con, store, docs)
    refs_by_accession = {
        d["accession_number"]: refs[d["sha256"]] for d in docs if d["sha256"] in refs
    }
    gold_set = gold_dir / GOLD_SET.name if gold_dir else None
    risk_labels = gold_dir / RISK_LABELS.name if gold_dir else None
    gold, labels = read_gold(gold_set), read_risk_labels(risk_labels)
    models = models or [r[0] for r in con.execute(
        "SELECT DISTINCT model FROM extracted.filings ORDER BY 1").fetchall()]  # fmt: skip
    run_id, now, results = uuid.uuid4().hex[:12], utcnow(), {}
    for model in models:
        scores, mismatches, n_labels = evaluate_model(con, model, refs_by_accession, gold, labels)
        per_label = {k[1]: s for k, s in scores.items() if k[1].startswith("risk_category:")}
        macro = macro_f1(per_label) if per_label else None
        version = con.execute(
            "SELECT string_agg(DISTINCT extractor_version, ' | ') FROM extracted.filings "
            "WHERE model = ?", [model]).fetchone()[0]  # fmt: skip
        con.execute("INSERT INTO eval.runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [run_id, model, version, XBRL_VERSION, len(gold), n_labels,
                     NUMERIC_TOLERANCE, now])  # fmt: skip
        for (reference, field), s in scores.items():
            r = s.as_row()
            con.execute(
                "INSERT INTO eval.field_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, model, reference, field, r["n"], r["tp"], r["fp"], r["fn"], r["exact"],
                 r["precision"], r["recall"], r["f1"], r["exact_match"]],
            )  # fmt: skip
        if macro is not None:
            con.execute(
                "INSERT INTO eval.field_scores (run_id, model, reference, field, f1) "
                "VALUES (?, ?, 'gold', 'risk_category:macro', ?)",
                [run_id, model, macro],
            )
        if mismatches:
            con.executemany("INSERT INTO eval.mismatches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [[run_id, model, *m] for m in mismatches])  # fmt: skip
        results[model] = {"scores": scores, "macro_f1": macro, "mismatches": mismatches,
                          "risk_labels": n_labels}  # fmt: skip
    return {"run_id": run_id, "gold_rows": len(gold), "label_rows": len(labels),
            "xbrl_docs": len(refs_by_accession), "models": results}  # fmt: skip


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--model", action="append", help="only this model (repeatable)")
    p.add_argument("--show", type=int, default=25, help="mismatches to print per model")
    args = p.parse_args()
    settings = get_settings()
    con = duckdb.connect(str(settings.duckdb_path))
    out = run(con, get_store(settings), args.model)

    rev = sum(1 for _ in con.execute(
        "SELECT 1 FROM eval.xbrl_reference WHERE xbrl_version = ? AND revenue_usd IS NOT NULL",
        [XBRL_VERSION]).fetchall())  # fmt: skip
    print(f"run {out['run_id']}: {out['xbrl_docs']} filings with an XBRL reference "
          f"({rev} with revenue), {out['gold_rows']} verified gold rows, "
          f"{out['label_rows']} labelled risks")  # fmt: skip
    if not out["gold_rows"]:
        print("No verified gold rows: run python -m evaluation.gold, check rows, set verified=y")
    for model, res in out["models"].items():
        print(f"\n## {model}\n")
        print(markdown_table(res["scores"], res["macro_f1"]))
        if res["mismatches"]:
            print(f"\nMismatches ({len(res['mismatches'])}, first {args.show}):")
            for ref, field, _acc, ticker, fy, pred, exp in res["mismatches"][: args.show]:
                print(f"  {ref:4} {field:15} {ticker or '?':6} {fy or '?'}  "
                      f"predicted {pred[:70]}  expected {exp}")  # fmt: skip


if __name__ == "__main__":
    main()
