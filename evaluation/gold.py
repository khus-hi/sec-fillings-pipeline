"""Build the gold set: python -m evaluation.gold

Picks 30 filings (3 companies per sector, one fiscal year each, fixed seed) and writes two
labelling files into evaluation/gold/:

- gold_set.csv: revenue and net income pre-filled from the filing's inline XBRL, the employee
  count pre-filled only when the company tags it, and the candidate employee sentences as
  evidence. A person checks each row against the filing, corrects it, and sets verified=y.
- risk_labels.csv: the risk titles the model chose for 10 of those filings, without the model's
  category, so the labeller picks a category blind.

Existing files are never overwritten without --force: they hold human labels.
See evaluation/LABELLING.md for the rules.
"""

from __future__ import annotations

import argparse
import csv
import random
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import duckdb

from evaluation.xbrl import XBRL_VERSION, Reference, reference_values
from extraction.context import employees_context
from ingestion.config import REPO_ROOT, get_settings, get_store
from ingestion.manifest import utcnow

GOLD_DIR = REPO_ROOT / "evaluation" / "gold"
GOLD_SET = GOLD_DIR / "gold_set.csv"
RISK_LABELS = GOLD_DIR / "risk_labels.csv"

PER_SECTOR = 3
RISK_LABEL_DOCS = 10
SEED = 2026

GOLD_COLUMNS = [
    "accession_number", "ticker", "sector", "fiscal_year", "period_end",
    "revenue_usd", "revenue_source", "net_income_usd", "net_income_source",
    "employee_count", "employee_source", "employee_evidence", "verified", "notes",
]  # fmt: skip
RISK_COLUMNS = ["accession_number", "ticker", "fiscal_year", "title", "gold_category"]

DDL = """
CREATE SCHEMA IF NOT EXISTS eval;
CREATE TABLE IF NOT EXISTS eval.xbrl_reference (
    sha256             VARCHAR NOT NULL,
    xbrl_version       VARCHAR NOT NULL,
    revenue_usd        BIGINT,
    revenue_concept    VARCHAR,
    net_income_usd     BIGINT,
    net_income_concept VARCHAR,
    employee_count     BIGINT,
    computed_at        TIMESTAMP NOT NULL,
    PRIMARY KEY (sha256, xbrl_version)
);
"""


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(DDL)


def documents(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """One row per parsed document (by content hash)."""
    rows = con.execute(
        """SELECT d.sha256, min(d.accession_number), min(d.ticker), min(d.sector),
                  min(d.report_date), min(d.raw_uri)
           FROM meta.documents d JOIN parsed.documents p USING (sha256)
           WHERE p.status = 'ok'
           GROUP BY d.sha256 ORDER BY 3, 5"""
    ).fetchall()
    keys = ["sha256", "accession_number", "ticker", "sector", "period_end", "raw_uri"]
    return [dict(zip(keys, r, strict=True)) for r in rows]


def sample(docs: list[dict], per_sector: int = PER_SECTOR, seed: int = SEED) -> list[dict]:
    """per_sector companies from each sector, one filing each, reproducible from the seed.
    The fiscal year is drawn per company, so the set spans all years."""
    rng = random.Random(seed)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for d in docs:
        by_ticker[d["ticker"]].append(d)
    by_sector: dict[str, list[str]] = defaultdict(list)
    for ticker, filings in by_ticker.items():
        by_sector[filings[0]["sector"] or "unknown"].append(ticker)
    picked = []
    for sector in sorted(by_sector):
        tickers = sorted(by_sector[sector])
        for ticker in sorted(rng.sample(tickers, min(per_sector, len(tickers)))):
            filings = sorted(by_ticker[ticker], key=lambda d: d["period_end"] or date.min)
            picked.append(filings[rng.randrange(len(filings))])
    return picked


def xbrl_references(con: duckdb.DuckDBPyConnection, store, docs: list[dict]) -> dict:
    """XBRL reference values for these documents, computed once per XBRL_VERSION and cached."""
    ensure_schema(con)
    cached = {
        r[0]: Reference(*r[1:])
        for r in con.execute(
            "SELECT sha256, revenue_usd, revenue_concept, net_income_usd, net_income_concept, "
            "employee_count FROM eval.xbrl_reference WHERE xbrl_version = ?",
            [XBRL_VERSION],
        ).fetchall()
    }
    for d in docs:
        if d["sha256"] in cached or d["period_end"] is None:
            continue
        ref = reference_values(store.get(store.key_from_uri(d["raw_uri"])), d["period_end"])
        con.execute(
            "INSERT OR REPLACE INTO eval.xbrl_reference VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [d["sha256"], XBRL_VERSION, ref.revenue_usd, ref.revenue_concept, ref.net_income_usd,
             ref.net_income_concept, ref.employee_count, utcnow()],
        )  # fmt: skip
        cached[d["sha256"]] = ref
    return cached


def employee_evidence(con: duckdb.DuckDBPyConnection, sha256: str, limit: int = 500) -> str:
    row = con.execute(
        "SELECT text FROM parsed.sections WHERE sha256 = ? AND section = 'item_1'", [sha256]
    ).fetchone()
    text = employees_context(row[0]) if row else ""
    return re.sub(r"\s+", " ", text).strip()[:limit]


def gold_rows(con: duckdb.DuckDBPyConnection, picked: list[dict], refs: dict) -> list[dict]:
    rows = []
    for d in picked:
        ref = refs.get(d["sha256"]) or Reference(None, None, None, None, None)
        rows.append({
            "accession_number": d["accession_number"], "ticker": d["ticker"],
            "sector": d["sector"], "fiscal_year": d["period_end"].year if d["period_end"] else "",
            "period_end": d["period_end"] or "",
            "revenue_usd": ref.revenue_usd if ref.revenue_usd is not None else "",
            "revenue_source": f"xbrl:{ref.revenue_concept}" if ref.revenue_concept else "",
            "net_income_usd": ref.net_income_usd if ref.net_income_usd is not None else "",
            "net_income_source": f"xbrl:{ref.net_income_concept}" if ref.net_income_concept else "",
            "employee_count": ref.employee_count if ref.employee_count is not None else "",
            "employee_source": "xbrl:dei:EntityNumberOfEmployees" if ref.employee_count else "",
            "employee_evidence": employee_evidence(con, d["sha256"]),
            "verified": "", "notes": "",
        })  # fmt: skip
    return rows


def risk_rows(con: duckdb.DuckDBPyConnection, picked: list[dict], model: str | None) -> list[dict]:
    rows = []
    for d in picked[:: max(len(picked) // RISK_LABEL_DOCS, 1)][:RISK_LABEL_DOCS]:
        found = con.execute(
            "SELECT fiscal_year, risk_factors FROM extracted.filings "
            "WHERE sha256 = ? AND (? IS NULL OR model = ?) ORDER BY model LIMIT 1",
            [d["sha256"], model, model],
        ).fetchone()
        if not found:
            continue
        for risk in found[1]:
            rows.append(
                {"accession_number": d["accession_number"], "ticker": d["ticker"],
                 "fiscal_year": found[0], "title": risk["title"], "gold_category": ""}
            )  # fmt: skip
    return rows


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--model", help="whose risk titles to label (default: any)")
    p.add_argument("--force", action="store_true", help="overwrite existing label files")
    args = p.parse_args()

    settings = get_settings()
    con = duckdb.connect(str(settings.duckdb_path))
    picked = sample(documents(con), seed=args.seed)
    refs = xbrl_references(con, get_store(settings), picked)
    for path, columns, rows in [
        (GOLD_SET, GOLD_COLUMNS, gold_rows(con, picked, refs)),
        (RISK_LABELS, RISK_COLUMNS, risk_rows(con, picked, args.model)),
    ]:
        if path.exists() and not args.force:
            print(f"{path.relative_to(REPO_ROOT)} exists, left as is (--force to overwrite)")
            continue
        write_csv(path, columns, rows)
        print(f"wrote {len(rows)} rows to {path.relative_to(REPO_ROOT)}")
    with_rev = sum(refs.get(d["sha256"]) is not None and refs[d["sha256"]].revenue_usd is not None
                   for d in picked)  # fmt: skip
    print(f"{len(picked)} filings sampled; XBRL revenue found for {with_rev}")


if __name__ == "__main__":
    main()
