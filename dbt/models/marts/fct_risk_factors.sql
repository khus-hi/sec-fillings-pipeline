-- One row per risk factor, with the company's sector and the category's label.
select
    r.sha256,
    r.model,
    r.accession_number,
    r.ticker,
    c.sector,
    r.fiscal_year,
    r.risk_rank,
    r.title,
    r.category,
    k.label as category_label
from {{ ref('stg_risk_factors') }} as r
join {{ ref('dim_companies') }} as c on c.ticker = r.ticker
join {{ ref('risk_categories') }} as k on k.category = r.category
