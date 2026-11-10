{# Generic test: no two rows share the same values in `columns` (the model's grain). #}
{% test unique_combination(model, columns) %}
select {{ columns | join(', ') }}, count(*) as n_rows
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
