import pytest

from parsing.chunking import chunk_text
from parsing.html_text import clean_text, html_to_text
from parsing.sections import extract_sections, find_headings
from tests.sample_10k import build_10k


@pytest.fixture(scope="module")
def text() -> str:
    return html_to_text(build_10k())


@pytest.fixture(scope="module")
def sections(text):
    return extract_sections(text)


# ---------- HTML to text ----------


def test_hidden_xbrl_header_is_dropped(text):
    assert "HIDDENXBRL" not in text
    assert "dei:EntityRegistrantName" not in text


def test_inline_xbrl_numbers_stay_inline(text):
    # The <ix:nonFraction> tag must not split "$391,035 million" across lines.
    assert "Total net sales were $391,035 million in 2024" in text
    assert "approximately 164,000 full-time equivalent employees" in text


def test_table_rows_become_single_lines(text):
    assert "Net sales | $ | 391,035 | $ | 383,285" in text


def test_page_furniture_removed(text):
    lines = text.splitlines()
    assert "Table of Contents" not in lines
    assert "17" not in lines  # bare page number


def test_nbsp_normalised(text):
    assert "\xa0" not in text
    assert "Item 1. Business" in text


def test_script_and_style_dropped():
    raw = b"<html><head><style>.x{color:red}</style></head><body><script>var a=1;</script>"
    raw += b"<p>Visible</p></body></html>"
    assert html_to_text(raw) == "Visible\n"


def test_clean_text_collapses_blank_lines():
    assert clean_text("a\n\n\n\n\nb") == "a\n\nb\n"


# ---------- Section finding ----------


def test_toc_and_body_headings_both_detected(text):
    items = [item for _, item in find_headings(text)]
    assert items.count("1a") == 2  # table of contents + real heading
    assert items.count("7a") == 2


def test_cross_reference_mid_sentence_is_not_a_heading(text):
    # "see Part I, Item 1A of this Form 10-K" must not count as a heading.
    starts = [text[pos : pos + 40] for pos, item in find_headings(text) if item == "1a"]
    assert all(s.startswith("Item 1A") for s in starts)


def test_all_wanted_sections_found(sections):
    assert set(sections) == {"item_1", "item_1a", "item_7", "item_8"}


def test_real_section_beats_table_of_contents(sections):
    item_1 = sections["item_1"].text
    assert item_1.startswith("Item 1. Business")
    assert "164,000" in item_1
    assert "Risk Factors\n" not in item_1.split("\n", 1)[0]


def test_section_boundaries(sections):
    risk = sections["item_1a"].text
    assert risk.startswith("Item 1A. Risk Factors")
    assert "supply chains" in risk
    assert "Unresolved Staff Comments" not in risk  # Item 1B ends it

    mdna = sections["item_7"].text
    assert "391,035" in mdna
    assert "Interest rate and foreign currency risk" not in mdna  # Item 7A ends it

    fin = sections["item_8"].text
    assert "Net income | $ | 93,736" in fin
    assert "Changes in and Disagreements" not in fin


def test_short_section_flag(sections):
    assert not sections["item_1a"].is_short
    assert sections["item_8"].is_short  # tiny in the fixture: would be flagged in a report


def test_missing_section_is_absent_not_wrong():
    text = "Item 1. Business\nWe sell things.\nItem 2. Properties\nOffices.\n"
    assert set(extract_sections(text)) == {"item_1"}


def test_items_7_and_7a_combined_heading():
    text = "Items 7 and 7A. MD&A and Market Risk\nRevenue grew.\nItem 8. Financials\nx\n"
    assert extract_sections(text)["item_7"].text.startswith("Items 7 and 7A")


# ---------- Chunking ----------


def test_short_text_is_one_chunk():
    chunks = chunk_text("one paragraph\n\nsecond paragraph", max_chars=1000, overlap_chars=100)
    assert len(chunks) == 1
    assert chunks[0].text == "one paragraph\nsecond paragraph"  # blank lines dropped


def test_chunks_respect_max_and_keep_paragraphs_whole():
    paras = [(f"Paragraph {i}. " + "word " * 40).strip() for i in range(30)]
    chunks = chunk_text("\n\n".join(paras), max_chars=1000, overlap_chars=250)
    assert len(chunks) > 1
    assert all(len(c.text) <= 1000 for c in chunks)
    for c in chunks:  # every chunk is made of complete paragraphs
        for piece in c.text.split("\n"):
            assert piece in paras


def test_overlap_repeats_boundary_paragraph():
    paras = [f"P{i} " + "x" * 300 for i in range(10)]
    chunks = chunk_text("\n\n".join(paras), max_chars=1000, overlap_chars=400)
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert prev.text.split("\n")[-1] == nxt.text.split("\n")[0]


def test_long_paragraph_split_on_sentences():
    para = " ".join(f"Sentence number {i} ends here." for i in range(200))
    chunks = chunk_text(para, max_chars=500, overlap_chars=50)
    assert all(len(c.text) <= 500 for c in chunks)
    assert all(c.text.endswith(".") for c in chunks)


def test_every_paragraph_appears_in_some_chunk():
    paras = [f"Fact number {i} " + "y" * (i * 37 % 500 + 1) for i in range(60)]
    chunks = chunk_text("\n\n".join(paras), max_chars=1500, overlap_chars=200)
    joined = "\n".join(c.text for c in chunks)
    assert all(p in joined for p in paras)


def test_big_table_is_split_between_rows():
    table = "\n".join(f"Row {i} | {i * 1000:,}" for i in range(400))
    chunks = chunk_text(table, max_chars=1000, overlap_chars=100)
    assert all(len(c.text) <= 1000 for c in chunks)
    assert "Row 399 | 399,000" in chunks[-1].text
    assert all("Row" in line for c in chunks for line in c.text.splitlines())  # no mid-row cut


def test_oversized_sentence_free_line_is_hard_cut():
    chunks = chunk_text("z" * 2500, max_chars=1000, overlap_chars=100)
    assert [len(c.text) for c in chunks] == [1000, 1000, 500]


def test_chunk_indices_and_token_estimate():
    chunks = chunk_text("\n".join("a" * 400 for _ in range(10)), max_chars=1000, overlap_chars=0)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert chunks[0].approx_tokens == len(chunks[0].text) // 4


def test_overlap_must_be_smaller_than_max():
    with pytest.raises(ValueError):
        chunk_text("x", max_chars=100, overlap_chars=100)


def test_real_sized_risk_section_chunks_fit_budget(sections):
    chunks = chunk_text(sections["item_1a"].text, max_chars=2000, overlap_chars=300)
    assert len(chunks) > 1
    assert all(c.approx_tokens <= 500 for c in chunks)
