-- A record exists exactly when all three LLM tasks are ok for that document and model
-- (Stage 4 deletes the record when a task is later quarantined), unless assembling the
-- record itself failed, which is quarantined under the task name "record".
with complete as (
    select sha256, model
    from {{ ref('stg_task_results') }}
    group by sha256, model
    having count(*) filter (where status = 'ok') = 3
),

record_failed as (
    select sha256, model
    from {{ source('extracted', 'quarantine') }}
    where task = 'record'
)

select coalesce(c.sha256, f.sha256) as sha256, coalesce(c.model, f.model) as model,
       c.sha256 is not null as tasks_complete, f.sha256 is not null as has_record
from complete as c
full outer join {{ ref('stg_filings') }} as f using (sha256, model)
where (c.sha256 is null or f.sha256 is null)
  and (coalesce(c.sha256, f.sha256), coalesce(c.model, f.model)) not in (select * from record_failed)
