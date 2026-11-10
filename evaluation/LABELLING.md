# Gold-set labelling rules

`python -m evaluation.gold` writes two files into `evaluation/gold/`. They hold human labels:
the script never overwrites them unless you pass `--force`.

## gold_set.csv (30 filings, 3 companies per sector)

Check each row against the filing itself. Use the EDGAR link or the raw document in the S3 raw
zone; `python -m parsing.inspect_doc <TICKER> --accession <n>` shows the parsed sections.

| Column | Rule |
|---|---|
| `revenue_usd` | Total revenue for the fiscal year ending `period_end`, in whole dollars, for the registrant itself (not a subsidiary in a combined filing). This is the top line of the income statement: "Total revenues", "Net sales", "Sales to customers", "Total net revenue" or "Operating revenues". If the statement shows both a sales line and a larger "Total revenues and other income", use the larger total (the top line). For banks, use total net revenue (net of interest expense). Pre-filled from the filing's XBRL; correct it if the tagged concept is not the top line, and set `revenue_source` to `manual`. |
| `net_income_usd` | Net income attributable to the company (the parent), same year. Negative for a loss. |
| `employee_count` | The company-wide total stated in Item 1 (Human Capital), not one segment or country. If the filing states no total, leave it empty: empty means "not in the filing", and a model answer there counts as a false positive. `employee_evidence` shows the candidate sentences. |
| `verified` | Set to `y` once the row is checked. Only verified rows are scored. |
| `notes` | Anything ambiguous, such as several revenue lines or a total given only as a range. |

## risk_labels.csv (the model's risk titles for 10 of those filings)

Pick the one category that best fits each title, using the definitions below. The model's own
category is deliberately not in the file. If two fit, pick the one that names the cause of the
risk (a cyberattack that disrupts supply is `cybersecurity_technology`).

| Category | Definition |
|---|---|
| `market_competition` | competitors, pricing pressure, demand, customer concentration |
| `macroeconomic` | recession, inflation, interest rates, currency exchange rates |
| `regulatory_legal` | laws, regulation, litigation, tax, antitrust, compliance |
| `operational_supply_chain` | suppliers, manufacturing, logistics, outages, quality |
| `cybersecurity_technology` | cyberattacks, data privacy, IT failures, AI, tech change |
| `financial_liquidity_credit` | debt, liquidity, credit ratings, capital, impairments |
| `geopolitical` | war, sanctions, trade policy, tariffs, political instability |
| `climate_environmental` | climate change, weather, emissions rules, energy transition |
| `human_capital` | hiring and keeping talent, key people, labor relations, unions |
| `strategic_reputational` | M&A, strategy execution, brand, reputation, ESG |
| `other` | anything that fits none of the above |
