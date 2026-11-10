-- One row per 10-K filing (accession number), with the company facts from the manifest.
select
    accession_number,
    sha256,
    cik,
    upper(ticker)        as ticker,
    company_name,
    sector,
    sic,
    sic_description,
    filing_date,
    report_date          as period_end,
    year(report_date)    as fiscal_year,
    source_url,
    size_bytes,
    ingested_at
from {{ source('meta', 'documents') }}
where form = '10-K'
