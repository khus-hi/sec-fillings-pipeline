-- One row per model: reliability, cost and field coverage of its extraction.
-- The basis for comparing models (e.g. a local and a hosted one) on the same documents.
with tasks as (
    select
        model,
        count(distinct sha256)                               as n_documents,
        count(*)                                             as n_tasks,
        count(*) filter (where status = 'ok')                as n_tasks_ok,
        count(*) filter (where status = 'quarantined')       as n_tasks_quarantined,
        sum(retries)                                         as n_retries,
        round(avg(latency_s), 2)                             as avg_task_latency_s,
        sum(prompt_tokens + completion_tokens)               as total_tokens,
        round(sum(cost_usd), 4)                              as total_cost_usd
    from {{ ref('stg_task_results') }}
    group by model
),

records as (
    select
        model,
        count(*)                                             as n_records,
        count(revenue_usd)                                   as n_with_revenue,
        count(net_income_usd)                                as n_with_net_income,
        count(employee_count)                                as n_with_employees,
        count(*) filter (where is_flagged)                   as n_flagged
    from {{ ref('fct_filings') }}
    group by model
)

select
    t.model,
    t.n_documents,
    t.n_tasks,
    t.n_tasks_ok,
    t.n_tasks_quarantined,
    round(t.n_tasks_ok / t.n_tasks, 4)                       as task_ok_rate,
    t.n_retries,
    t.avg_task_latency_s,
    t.total_tokens,
    t.total_cost_usd,
    coalesce(r.n_records, 0)                                 as n_records,
    round(r.n_with_revenue / r.n_records, 4)                 as revenue_coverage,
    round(r.n_with_net_income / r.n_records, 4)              as net_income_coverage,
    round(r.n_with_employees / r.n_records, 4)               as employee_coverage,
    coalesce(r.n_flagged, 0)                                 as n_flagged
from tasks as t
left join records as r using (model)
