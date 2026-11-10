-- One row per company (ticker). Name and industry come from the most recent filing, so a
-- renamed company shows its current name.
select
    ticker,
    arg_max(cik, filing_date)               as cik,
    arg_max(company_name, filing_date)      as company_name,
    arg_max(sector, filing_date)            as sector,
    arg_max(sic, filing_date)               as sic,
    arg_max(sic_description, filing_date)   as sic_description,
    count(*)                                as n_filings,
    min(fiscal_year)                        as first_fiscal_year,
    max(fiscal_year)                        as last_fiscal_year
from {{ ref('stg_documents') }}
group by ticker
