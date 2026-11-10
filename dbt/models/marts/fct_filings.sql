-- One row per filing and model: the extracted figures plus the ratios built on them.
-- Year-over-year growth is only computed between consecutive fiscal years.
with filings as (
    select
        f.*,
        d.filing_date,
        c.sector
    from {{ ref('stg_filings') }} as f
    join {{ ref('stg_documents') }} as d using (accession_number)
    join {{ ref('dim_companies') }} as c on c.ticker = f.ticker
),

with_prior as (
    select
        *,
        lag(fiscal_year) over w      as prior_fiscal_year,
        lag(revenue_usd) over w      as prior_revenue_usd,
        lag(employee_count) over w   as prior_employee_count
    from filings
    window w as (partition by ticker, model order by fiscal_year)
)

select
    sha256,
    model,
    accession_number,
    ticker,
    company_name,
    sector,
    fiscal_year,
    period_end,
    filing_date,
    revenue_usd,
    net_income_usd,
    employee_count,
    round(net_income_usd / nullif(revenue_usd, 0), 4)            as net_margin,
    round(revenue_usd / nullif(employee_count, 0), 0)            as revenue_per_employee_usd,
    case when prior_fiscal_year = fiscal_year - 1
         then round(revenue_usd / nullif(prior_revenue_usd, 0) - 1, 4)
    end                                                          as revenue_growth_yoy,
    case when prior_fiscal_year = fiscal_year - 1
         then employee_count - prior_employee_count
    end                                                          as employee_change_yoy,
    n_risk_factors,
    n_warnings,
    n_warnings > 0                                               as is_flagged,
    extractor_version,
    extracted_at
from with_prior
