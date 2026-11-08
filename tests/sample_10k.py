"""A small synthetic 10-K in inline-XBRL style, with the traps real filings have:

- a hidden <ix:header> block full of machine data that must not leak into the text
- a table of contents that repeats every Item heading
- page numbers and "Table of Contents" back-links on every page
- numbers wrapped in <ix:nonFraction> tags and &nbsp; inside headings
- a sentence that cross-references "Item 1A" mid-line
- "Item 7" and "Item 7A" headings (7A must not end up inside Item 1's search for "7")
"""

RISK = (
    "<p><b>Our business depends on global supply chains.</b> Disruptions at suppliers, "
    "including those caused by natural disasters, pandemics or trade restrictions, could "
    "materially affect our ability to manufacture and ship products on time.</p>"
)
PAGE_BREAK = '<p style="text-align:center">{n}</p><hr/><p><a href="#toc">Table of Contents</a></p>'


def build_10k(n_risks: int = 25) -> bytes:
    risks = "\n".join(RISK for _ in range(n_risks))
    html = f"""<html><head><title>aapl-10k</title><style>p {{margin:0}}</style></head><body>
<div style="display: none"><ix:header><ix:hidden>HIDDENXBRL dei:EntityRegistrantName
Example Corp</ix:hidden></ix:header></div>
<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>
<p>FORM 10-K</p>
<p>Example Corp</p>
<div id="toc"><table>
<tr><td>Item&#160;1.</td><td>Business</td><td>1</td></tr>
<tr><td>Item 1A.</td><td>Risk Factors</td><td>5</td></tr>
<tr><td>Item 1B.</td><td>Unresolved Staff Comments</td><td>17</td></tr>
<tr><td>Item 7.</td><td>Management's Discussion and Analysis</td><td>20</td></tr>
<tr><td>Item 7A.</td><td>Quantitative and Qualitative Disclosures About Market Risk</td>
<td>30</td></tr>
<tr><td>Item 8.</td><td>Financial Statements and Supplementary Data</td><td>31</td></tr>
<tr><td>Item 9.</td><td>Changes in and Disagreements with Accountants</td><td>60</td></tr>
</table></div>
{PAGE_BREAK.format(n=1)}
<p><b>PART I</b></p>
<p><b>Item&#160;1.&#160;&#160;&#160;&#160;Business</b></p>
<p>Example Corp designs, manufactures and markets devices. For a discussion of the risks,
see Part I, Item 1A of this Form 10-K under the heading "Risk Factors."</p>
<p><b>Human Capital</b></p>
<p>As of September 28, 2024, the Company had approximately
<ix:nonFraction name="dei:EntityNumberOfEmployees">164,000</ix:nonFraction>
full-time equivalent employees.</p>
{PAGE_BREAK.format(n=2)}
<p><b>Item 1A.    Risk Factors</b></p>
{risks}
<p><b>Item 1B.    Unresolved Staff Comments</b></p>
<p>None.</p>
{PAGE_BREAK.format(n=17)}
<p><b>PART II</b></p>
<p><b>Item 7.    Management's Discussion and Analysis of Financial Condition and Results
of Operations</b></p>
<p>Total net sales were $<ix:nonFraction name="us-gaap:Revenues" scale="6">391,035</ix:nonFraction>
million in 2024, compared to $383,285 million in 2023.</p>
<p>Net income was $93,736 million.</p>
<p><b>Item 7A.    Quantitative and Qualitative Disclosures About Market Risk</b></p>
<p>Interest rate and foreign currency risk.</p>
<p><b>Item 8.    Financial Statements and Supplementary Data</b></p>
<table>
<tr><td></td><td>2024</td><td>2023</td></tr>
<tr><td>Net sales</td><td>$</td><td>391,035</td><td>$</td><td>383,285</td></tr>
<tr><td>Net income</td><td>$</td><td>93,736</td><td>$</td><td>96,995</td></tr>
</table>
<p><b>Item 9.    Changes in and Disagreements with Accountants</b></p>
<p>None.</p>
</body></html>"""
    return html.encode()
