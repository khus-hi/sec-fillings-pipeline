"""Turn a 10-K's HTML into plain text that keeps the document's structure.

Modern 10-Ks are inline XBRL (iXBRL): ordinary HTML with machine-readable tags woven
in, plus a hidden <ix:header> block of metadata that can be megabytes long. A naive
"strip all tags" would dump that hidden block into the text, so we skip anything the
browser would not show.

Output rules:
  - block elements (p, div, tr, li, headings, br) end a line
  - table cells are joined with " | " so a row stays on one line
  - page furniture (bare page numbers, "Table of Contents" links) is dropped
"""

from __future__ import annotations

import re

from lxml import etree, html

# Elements whose text is never shown on screen.
SKIP_TAGS = {"script", "style", "head", "title", "noscript", "ix:header", "xbrli:context"}

# Elements that start a new line when they end.
BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "ul", "ol", "table", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "body", "center", "blockquote",
}  # fmt: skip
CELL_TAGS = {"td", "th"}

_WS_RE = re.compile(r"\s+")
_HIDDEN_RE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
_PAGE_NUMBER_RE = re.compile(r"^(page\s+)?[ivxlc\d]{1,4}$", re.IGNORECASE)
_FURNITURE = {"table of contents", "index"}


def _ws(s: str) -> str:
    # As in a browser, newlines and runs of spaces inside HTML text are just one space.
    # Only tags decide where lines break.
    return _WS_RE.sub(" ", s)


def _is_hidden(el) -> bool:
    return bool(_HIDDEN_RE.search(el.get("style") or ""))


def html_to_text(raw: bytes) -> str:
    """Convert raw filing bytes (HTML / iXBRL) to cleaned plain text."""
    root = html.fromstring(raw)
    parts: list[str] = []

    walker = etree.iterwalk(root, events=("start", "end"))
    for event, el in walker:
        tag = el.tag if isinstance(el.tag, str) else ""  # comments have non-str tags
        tag = tag.lower()
        if event == "start":
            if not tag or tag in SKIP_TAGS or _is_hidden(el):
                walker.skip_subtree()
                # skip_subtree means no "end" event for this element, but its tail is
                # visible text that belongs to the parent, so keep it.
                if el.tail:
                    parts.append(_ws(el.tail))
                continue
            if el.text:
                parts.append(_ws(el.text))
        else:  # end
            if tag in CELL_TAGS:
                parts.append(" | ")
            elif tag in BLOCK_TAGS:
                parts.append("\n")
            if el.tail:
                parts.append(_ws(el.tail))

    return clean_text("".join(parts))


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ").replace("​", "").replace("\r", "")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        # Tidy table rows: drop empty cells and the trailing separator.
        if " | " in line or line.endswith("|"):
            cells = [c.strip() for c in line.split("|")]
            line = " | ".join(c for c in cells if c)
        if _PAGE_NUMBER_RE.match(line) or line.lower() in _FURNITURE:
            continue
        lines.append(line)
    # Collapse runs of blank lines to a single blank line (a paragraph break).
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return out.strip() + "\n"
