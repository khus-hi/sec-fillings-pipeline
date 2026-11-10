-- One validated record per document and model. Lists are reduced to counts here;
-- the risk factors themselves are unnested in stg_risk_factors.
select
    sha256,
    model,
    accession_number,
    cik,
    upper(ticker)                         as ticker,
    company_name,
    period_end,
    fiscal_year,
    revenue_usd,
    net_income_usd,
    employee_count,
    coalesce(len(risk_factors), 0)        as n_risk_factors,
    coalesce(len(warnings), 0)            as n_warnings,
    warnings,
    extractor_version,
    extracted_at
from {{ source('extracted', 'filings') }}
