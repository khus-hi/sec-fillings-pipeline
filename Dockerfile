# Astro Runtime = Airflow 3 + Astronomer's defaults (same base as Project 1). Its ONBUILD steps
# copy the repo in and pip-install requirements.txt (the pipeline's own dependencies).
FROM astrocrpublic.azurecr.io/runtime:3.3-12

# dbt gets its own virtualenv so its pinned dependencies can never conflict with Airflow's.
# duckdb is the same version on both sides: the pipeline and dbt write the same file.
RUN python -m venv /usr/local/airflow/dbt_venv && \
    /usr/local/airflow/dbt_venv/bin/pip install --no-cache-dir \
        dbt-core==1.12.6 dbt-duckdb==1.11.1 duckdb==2.0.0

ENV DBT_EXECUTABLE=/usr/local/airflow/dbt_venv/bin/dbt \
    PYTHONPATH=/usr/local/airflow
