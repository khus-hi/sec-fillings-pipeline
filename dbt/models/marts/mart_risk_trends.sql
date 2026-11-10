-- Which risks companies cite, by sector and fiscal year: the share of filings in each
-- sector-year that list a category at least once. Every category appears for every
-- sector-year (share 0 when no filing cites it), so charts and year-over-year comparisons
-- never silently drop a category.
with groups as (
    select sector, fiscal_year, model, count(*) as n_filings
    from {{ ref('fct_filings') }}
    group by all
),

citing as (
    select sector, fiscal_year, model, category, count(distinct sha256) as n_filings_citing
    from {{ ref('fct_risk_factors') }}
    group by all
)

select
    g.sector,
    g.fiscal_year,
    g.model,
    k.category,
    k.label                                                  as category_label,
    g.n_filings,
    coalesce(c.n_filings_citing, 0)                          as n_filings_citing,
    round(coalesce(c.n_filings_citing, 0) / g.n_filings, 4)  as share_of_filings
from groups as g
cross join {{ ref('risk_categories') }} as k
left join citing as c
    on c.sector is not distinct from g.sector
    and c.fiscal_year = g.fiscal_year
    and c.model = g.model
    and c.category = k.category
