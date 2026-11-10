-- Each sector-year's filing count in the trends mart must equal the filings in fct_filings:
-- a join that dropped or duplicated filings would show up here.
with expected as (
    select sector, fiscal_year, model, count(*) as n_filings
    from {{ ref('fct_filings') }}
    group by all
)

select t.sector, t.fiscal_year, t.model, t.n_filings, e.n_filings as expected
from {{ ref('mart_risk_trends') }} as t
join expected as e
    on e.sector is not distinct from t.sector
    and e.fiscal_year = t.fiscal_year
    and e.model = t.model
where t.n_filings <> e.n_filings
