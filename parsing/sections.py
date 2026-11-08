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


@dataclass(frozen=True)
class Section:
    item: str  # "1", "1a", "7", "8"
    start: int  # character offset in the full text
    end: int
    text: str

    @property
    def name(self) -> str:
        return f"item_{self.item}"

    @property
    def is_short(self) -> bool:
        return len(self.text) < SHORT_SECTION_CHARS


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
        if best:
            start, end = best
            sections[f"item_{item}"] = Section(item, start, end, text[start:end].strip())
    return sections
