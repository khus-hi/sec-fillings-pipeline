"""List the revenue and net-income facts one filing tags in inline XBRL:
python -m evaluation.inspect_xbrl AXP [--year 2023]

Shows every candidate fact for the registrant's fiscal year (no dimensions) and which one the
reference picks, so a disagreement with the model can be checked against the filing's own tags.
"""

from __future__ import annotations

import argparse

import duckdb

from evaluation.gold import documents
from evaluation.xbrl import (
    NET_INCOME_CONCEPTS,
    REVENUE_CONCEPTS,
    _annual,
    read_xbrl,
    reference_values,
)
from ingestion.config import get_settings, get_store

_WORDS = ("revenue", "sales", "netincome", "profitloss")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("ticker")
    p.add_argument("--year", type=int, help="fiscal year (default: the newest)")
    p.add_argument("--all", action="store_true", help="every revenue/income-like concept")
    args = p.parse_args()

    settings = get_settings()
    con = duckdb.connect(str(settings.duckdb_path), read_only=True)
    docs = [d for d in documents(con) if d["ticker"] == args.ticker.upper()]
    if args.year:
        docs = [d for d in docs if d["period_end"] and d["period_end"].year == args.year]
    if not docs:
        raise SystemExit(f"no parsed filing for {args.ticker} {args.year or ''}")
    doc = max(docs, key=lambda d: d["period_end"])
    store = get_store(settings)
    raw = store.get(store.key_from_uri(doc["raw_uri"]))

    contexts, facts = read_xbrl(raw)
    known = set(REVENUE_CONCEPTS) | set(NET_INCOME_CONCEPTS)
    print(f"{doc['ticker']} {doc['accession_number']} period end {doc['period_end']}")
    print("Registrant facts for the fiscal year (no dimensions):")
    seen = set()
    for f in facts:
        ctx = contexts.get(f.context_id)
        if ctx is None or not _annual(ctx, doc["period_end"]):
            continue
        local = f.concept.split(":")[-1].lower()
        if f.concept not in known and not (args.all and any(w in local for w in _WORDS)):
            continue
        if (f.concept, f.value) in seen:
            continue
        seen.add((f.concept, f.value))
        mark = "*" if f.concept in known else " "
        print(f"  {mark} {f.concept:70} {int(f.value):>18,}")
    ref = reference_values(raw, doc["period_end"])
    print(f"Reference: revenue {ref.revenue_usd} ({ref.revenue_concept}), "
          f"net income {ref.net_income_usd} ({ref.net_income_concept})")  # fmt: skip
    print("* = a concept the reference considers; --all shows other revenue/income-like tags")


if __name__ == "__main__":
    main()
