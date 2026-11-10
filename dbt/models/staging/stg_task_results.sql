-- One row per document, LLM task and model, with the quarantine reason when it failed.
select
    t.sha256,
    t.task,
    t.model,
    t.status,
    t.attempts,
    t.attempts - 1                 as retries,
    t.latency_s,
    t.prompt_tokens,
    t.completion_tokens,
    t.cost_usd,
    t.context_chars,
    coalesce(len(t.warnings), 0)   as n_warnings,
    q.reason                       as quarantine_reason,
    t.extracted_at
from {{ source('extracted', 'task_results') }} as t
left join {{ source('extracted', 'quarantine') }} as q
    on q.sha256 = t.sha256 and q.task = t.task and q.model = t.model
