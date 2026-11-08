"""Find the 10-K sections the extraction stage needs.

A 10-K is organised as numbered Items. We keep four:

    item_1   Business            (employee count lives in "Human Capital")
    item_1a  Risk Factors        (risk factors to categorise)
    item_7   MD&A                (management's discussion of revenue, net income)
    item_8   Financial Statements (the reported numbers)

The hard part: every Item heading appears at least twice, once in the table of
contents and once at the real section. And the text cross-references items
("see Item 1A") all the time. Two rules handle this:

1. Only a line that *starts* with "Item N" and is short counts as a heading, so a
   sentence that mentions an item mid-line is ignored.
2. For each item we try every heading candidate and keep the one that produces the
   longest section. A table-of-contents entry is followed almost immediately by the
   next entry, so it always loses to the real heading.

Fallback: some filers keep "Item 7" / "Item 8" as a one-line pointer ("see pages 40-90")
and put the real text under a plain title such as "Management's Discussion and Analysis"
or "Report of Independent Registered Public Accounting Firm" (often in F-pages after
Item 15). When the heading-based section is short, we also try title lines and keep the
longer result. `Section.found_by` records which rule won.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The order of Items in Form 10-K. A section ends where the next later Item starts.
ITEM_ORDER = [
    "1", "1a", "1b", "1c", "2", "3", "4", "5", "6", "7", "7a", "8", "9", "9a", "9b", "9c",
    "10", "11", "12", "13", "14", "15", "16",
]  # fmt: skip
WANTED = ["1", "1a", "7", "8"]
_RANK = {item: i for i, item in enumerate(ITEM_ORDER)}

# "Item 1A." / "ITEM 1A:" / "Item 1A -" / "Items 7 and 7A." at the start of a line.
_HEADING_RE = re.compile(r"^\s*items?\s+(\d{1,2}[a-c]?)\b\s*[.:\-–—]?", re.IGNORECASE)
MAX_HEADING_LEN = 200  # a heading line is short; a sentence mentioning an item is not
SHORT_SECTION_CHARS = 2_000  # below this, flag: probably "incorporated by reference"

# Title lines that start a section when the "Item N" heading doesn't lead to the real text.
# Matched at the start of a short line, like headings, so cross-references are ignored.
_TITLES = {
    "1a": [r"risk\s+factors\s*\.?\s*$"],
    "7": [r"management.{0,3}s\s+discussion\s+and\s+analysis\b"],
    "8": [
        r"financial\s+statements\s+and\s+supplementary\s+data\b",
        r"reports?\s+of\s+independent\s+registered\s+public\s+accounting\s+firm\b",
    ],
}
_TITLE_RES = {
    item: re.compile(r"^\s*(?:" + "|".join(pats) + ")", re.IGNORECASE)
    for item, pats in _TITLES.items()
}


@dataclass(frozen=True)
class Section:
    item: str  # "1", "1a", "7", "8"
    start: int  # character offset in the full text
    end: int
    text: str
    found_by: str = "heading"  # heading | title

    @property
    def name(self) -> str:
        return f"item_{self.item}"

    @property
    def is_short(self) -> bool:
        return len(self.text) < SHORT_SECTION_CHARS


def _line_starts(text: str, max_len: int, test) -> list[int]:
    """Offsets of lines no longer than max_len for which test(line) is true."""
    found, offset = [], 0
    for line in text.split("\n"):
        if len(line) <= max_len and test(line):
            found.append(offset)
        offset += len(line) + 1
    return found


def find_headings(text: str) -> list[tuple[int, str]]:
    """All (offset, item) heading candidates, in document order."""
    found = []
    offset = 0
    for line in text.split("\n"):
        if len(line) <= MAX_HEADING_LEN:
            m = _HEADING_RE.match(line)
            if m and m.group(1).lower() in _RANK:
                found.append((offset, m.group(1).lower()))
        offset += len(line) + 1
    return found


def find_titles(text: str, item: str) -> list[int]:
    """Offsets of title lines for an item (see _TITLES), in document order."""
    regex = _TITLE_RES.get(item)
    if regex is None:
        return []
    return _line_starts(text, MAX_HEADING_LEN, regex.match)


def _longest(starts: list[int], ends: list[int], text_len: int) -> tuple[int, int] | None:
    """For each start, the section runs to the first end after it; return the longest."""
    best = None
    for start in starts:
        end = next((e for e in ends if e > start), text_len)
        if best is None or end - start > best[1] - best[0]:
            best = (start, end)
    return best


def extract_sections(text: str, wanted: list[str] = WANTED) -> dict[str, Section]:
    headings = find_headings(text)
    sections: dict[str, Section] = {}
    for item in wanted:
        best: tuple[int, int] | None = None
        for i, (start, this_item) in enumerate(headings):
            if this_item != item:
                continue
            # The section runs until the next heading of a *later* Item. Repeated
            # headings of the same or earlier Items (page headers, sub-references)
            # don't end it.
            end = next(
                (pos for pos, other in headings[i + 1 :] if _RANK[other] > _RANK[item]),
                len(text),
            )
            if best is None or end - start > best[1] - best[0]:
                best = (start, end)
        found_by = "heading"

        if best is None or len(text[best[0] : best[1]].strip()) < SHORT_SECTION_CHARS:
            # A title-based section ends at the next later Item heading, or at the title
            # of a later wanted Item (filers without usable headings have neither).
            ends = sorted(
                [pos for pos, other in headings if _RANK[other] > _RANK[item]]
                + [p for later in ITEM_ORDER[_RANK[item] + 1 :] for p in find_titles(text, later)]
            )
            alt = _longest(find_titles(text, item), ends, len(text))
            if alt and (best is None or alt[1] - alt[0] > best[1] - best[0]):
                best, found_by = alt, "title"

        if best:
            start, end = best
            sections[f"item_{item}"] = Section(item, start, end, text[start:end].strip(), found_by)
    return sections
