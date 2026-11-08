"""Debug section finding for one company: python -m parsing.inspect_doc GE [--accession ...]

Shows the heading candidates the parser saw, the section it picked for each Item, and
every other line that mentions an Item (to reveal headings the regex missed).
"""

from __future__ import annotations

import argparse
import re

import duckdb

from ingestion.config import get_settings, get_store
from parsing.html_text import html_to_text
from parsing.sections import extract_sections, find_headings

_MENTION_RE = re.compile(r"\bitems?\s*(1a|1|7|8)\b", re.IGNORECASE)
_TITLE_RE = re.compile(
    r"(risk factors|management.s discussion and analysis|financial statements and supplementary"
    r"|report of independent registered public accounting firm|human capital)",
    re.IGNORECASE,
)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("ticker")
    p.add_argument("--accession", help="a specific filing (default: the newest)")
    p.add_argument("--max-lines", type=int, default=25)
    args = p.parse_args()

    settings = get_settings()
    con = duckdb.connect(str(settings.duckdb_path), read_only=True)
    row = con.execute(
        "SELECT raw_uri, accession_number FROM meta.documents WHERE ticker = ? "
        "AND (? IS NULL OR accession_number = ?) ORDER BY filing_date DESC LIMIT 1",
        [args.ticker.upper(), args.accession, args.accession],
    ).fetchone()
    if not row:
        raise SystemExit(f"no document for {args.ticker}")
    store = get_store(settings)
    text = html_to_text(store.get(store.key_from_uri(row[0])))
    print(f"== {args.ticker} {row[1]}: {len(text):,} chars")

    print("\n-- heading candidates (offset, item, line)")
    for pos, item in find_headings(text):
        print(f"{pos:>9,}  {item:<4} {text[pos : pos + 90].splitlines()[0]!r}")

    print("\n-- picked sections")
    for name, sec in extract_sections(text).items():
        print(f"{name:<8} {len(sec.text):>8,} chars  {sec.found_by:<7} starts {sec.text[:100]!r}")

    def show(title: str, regex: re.Pattern) -> None:
        print(f"\n-- {title} (first {args.max_lines})")
        offset, shown = 0, 0
        for line in text.split("\n"):
            if regex.search(line) and shown < args.max_lines:
                print(f"{offset:>9,}  len={len(line):<5} {line[:110]!r}")
                shown += 1
            offset += len(line) + 1

    show("lines mentioning Item 1/1A/7/8 anywhere", _MENTION_RE)
    show("lines with a section title", _TITLE_RE)


if __name__ == "__main__":
    main()
