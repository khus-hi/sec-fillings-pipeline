# Running the pipeline in Airflow (Astro CLI)

The repository root is an [Astro](https://www.astronomer.io/docs/astro/cli/overview) project.
`astro dev start` runs Airflow 3 locally in Docker: scheduler, DAG processor, triggerer, API
server and UI, and a Postgres metadata database. Free, and entirely on your machine.

## Before you start

1. Docker Desktop running, and the Astro CLI installed (`brew install astro`).
2. The local S3 server running: `docker compose up -d`.
3. The Ollama app open, with the model pulled (`ollama pull qwen3:8b`).
4. `.env` set up as in the README. Astro passes `.env` into the containers as environment
   variables, so nothing secret is baked into the image.
5. Nothing else holding the warehouse: close a DuckDB shell or the Streamlit app that has
   `data/warehouse.duckdb` open, since DuckDB allows one writer.

## Start and run

```
astro dev start
```

The first build takes a few minutes. Open the Airflow UI URL it prints, find
`sec_filings_pipeline`, and trigger it. Stop everything with `astro dev stop`.

## How the containers reach your Mac

[`docker-compose.override.yml`](../docker-compose.override.yml) is merged into Astro's own
compose file. Tasks run in the scheduler container, so it gets:

| Setting | Why |
|---|---|
| `./data` mounted at `/usr/local/airflow/data` | Airflow and the command line use the same warehouse and local raw zone. |
| `S3_ENDPOINT_URL=http://host.docker.internal:8333` | `localhost` inside a container is the container itself; this is the Mac's SeaweedFS. |
| `OLLAMA_URL=http://host.docker.internal:11434` | the Ollama app on the Mac, where the GPU is. |

Code changes need `astro dev restart`, because the image copies the code at build time.

## The DAG: `sec_filings_pipeline`

```
ingest -> parse -> extract -> evaluate -> dbt_build
                      ^
        check_ollama -+
```

| Feature | How |
|---|---|
| Schedule | Mondays 06:00 UTC. 10-Ks arrive mostly in February and March; a week with nothing new finishes in seconds with no LLM calls, because every stage skips work already done. `catchup=False`: EDGAR always returns the latest filings, so past weeks add nothing. |
| Retries | 2 with exponential backoff (5 min, then 10). `check_ollama` gets 3 one minute apart, since the app may still be starting. `parse` and `dbt_build` get 0: a parser bug or a failing data test gives the same result on retry. |
| Failure policy | `ingest` fails if any company could not be ingested; `parse` fails if any document could not be parsed. Everything that succeeded is saved first, so a retry redoes only the failures. A quarantined LLM task does not fail `extract`: it is stored with its reason and scored as a miss. An unreachable model server does, so Airflow retries it. |
| Fail fast | `check_ollama` runs alongside ingestion and stops the run before extraction if the server is down or the model isn't pulled, with the command that fixes it. |
| Timeouts | `extract` 6 hours, the whole run 8 hours (a full re-extraction of 150 filings takes about 40 minutes on a MacBook Air). |
| One writer | `max_active_runs=1`, and each step opens and closes its own DuckDB connection. |
| dbt | Its own virtualenv in the image (see `Dockerfile`), so its dependencies can't clash with Airflow's. Artifacts go to `data/dbt/`. |

The steps themselves are plain functions in [`pipeline/steps.py`](../pipeline/steps.py), tested
without Airflow. CI also imports the DAG with the Airflow version in the Astro image and checks
its wiring ([`tests/test_dag_integrity.py`](../tests/test_dag_integrity.py)).
