-- One row per risk factor: the model lists up to 8 per filing, most material first.
with unnested as (
    select
        sha256,
        model,
        accession_number,
        upper(ticker)                          as ticker,
        fiscal_year,
        unnest(risk_factors)                   as risk,
        generate_subscripts(risk_factors, 1)   as risk_rank
    from {{ source('extracted', 'filings') }}
)

select
    sha256,
    model,
    accession_number,
    ticker,
    fiscal_year,
    risk_rank,
    risk.title       as title,
    risk.category    as category
from unnested
