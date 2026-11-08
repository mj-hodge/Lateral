# World Bank Indicator Pipeline

Pulls four World Bank indicators (population, GDP, GDP per capita, life expectancy) for 9 countries, 2018-2024. It lands raw JSON, validates the schema, stages to pandas/PSV, runs a `row_count` data-quality test, archives the landing files, upserts into base tables, then runs a DuckDB SQL analysis (life expectancy by GDP per capita per country).

## Repo layout

```
config/pipeline.yaml    all countries, indicators, dates, paths, retry settings
src/                    api, ingest, schema_check, staging, dq_tests, archive, merge, analysis, run_pipeline
storage/{landing,staging,archived,data}/<indicator-code>/
log/<indicator-code>/<request_id>.log     one request log per pull; log/pipeline.log = run log
tests/                  pytest unit tests; tests/<indicator-code>/row_count/ = pipeline DQ reports
sql/code/analysis.sql   the analysis query
sql/analysis/           analysis output (.psv)
plan/plan.html          design plan
```

## Setup

Run everything from the repo root (`/home/hodge/Lateral`).

Use the existing venv:

```bash
source .venv/bin/activate
```

Or recreate it (needs Python 3.10+; the existing venv uses 3.14.4):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt     # requests, pandas, duckdb, pyyaml, pytest, numpy
```

Without activating, prefix commands with `.venv/bin/python`.

## Run

```bash
python -m src.run_pipeline --config config/pipeline.yaml
```

This runs, per indicator in config order: ingest, schema check, staging, row_count test, archive, merge. It then runs the SQL analysis. Expect about 40 seconds and 108 API requests (9 countries x 4 indicators x 3 pages).

Flags:

| Flag | Effect |
|---|---|
| `--indicator CODE [CODE ...]` | only run these indicator codes, e.g. `--indicator SP.POP.TOTL NY.GDP.PCAP.CD` |
| `--skip-analysis` | skip the SQL analysis step |
| `--refetch` | ignore resume; re-pull every country/indicator pair |
| `--log-level LEVEL` | default `INFO` |

Note: the analysis needs the `SP.DYN.LE00.IN` and `NY.GDP.PCAP.CD` base tables to exist. If you ran with `--indicator` before those were built, run the full pipeline first.

## Tests

```bash
python -m pytest -q          # 539 tests, ~8s, no network needed
```

## Outputs

| What | Where |
|---|---|
| Raw landing JSON (empty after a successful run) | `storage/landing/<indicator>/<country>_<indicator>_<request_id>.json` |
| Staging | `storage/staging/<indicator>/<indicator>_staging.psv` |
| Archived landing files | `storage/archived/<indicator>/` |
| Base tables (upserted) | `storage/data/<indicator>/<indicator>.psv` |
| Request logs / run log | `log/<indicator>/<request_id>.log`, `log/pipeline.log` |
| row_count reports | `tests/<indicator>/row_count/` |
| **Final analysis result** | `sql/analysis/life_expectancy_by_gdp_per_capita.psv` |
| Analysis query | `sql/code/analysis.sql` |

All `.psv` files are pipe-delimited. View the result with: `column -s'|' -t sql/analysis/life_expectancy_by_gdp_per_capita.psv`

## Add a country or indicator (YAML only)

Edit `config/pipeline.yaml`; no code changes.

- Country: append its ISO3 code to `countries`.
- Indicator: add an entry under `indicators` with `code`, `name`, `table_name`, `primary_key`.

Then run the pipeline. Date range, paths and retry settings are also in the YAML. (The analysis itself only uses the two indicators in `analysis.tables`.)

## Reruns and idempotency

Rerunning is safe. The merge is an upsert on the primary key, so business values do not change when the source data is unchanged. What changes each run: `request_id`, `staging_merge_id`, and the lineage list columns (`request_id_linage`, `staging_merged_id_linage`), which gain the newest id (an id is never appended twice). Previous landing files are in `storage/archived/`.

## Failures and resuming

- **429, 5xx, connection errors:** sleep 60s and retry, up to 3 retries; then the run fails with a clear error.
- **Schema check failure:** that pull stops loudly; nothing is silently dropped.
- **row_count failure:** the run halts before archive and merge. Landing files stay in place.
- **Resume:** just rerun the same command. Country/indicator pairs already landed and complete are skipped; incomplete or failed pairs are re-pulled; stale files go to `storage/rejected/`. Use `--refetch` to force a full re-pull.

Exit codes: `0` ok, `1` unexpected, `2` config, `3` API/retry exhausted, `4` schema check, `5` row_count failure, `6` staging, `7` merge, `8` file collision, `9` analysis. Details are in `log/pipeline.log`.

## Assumptions about your local setup

- Python 3.10+ (tested with 3.14.4 in `.venv`; Python 3.14 bytecode is in `src/__pycache__`).
- Developed on WSL2 Linux with bash; any POSIX shell should work. Windows users: use `.venv\Scripts\activate`.
- Outbound network access to `https://api.worldbank.org` (the unit tests need none).
- Commands are run from the repo root; relative paths in the config resolve from the repo root.
- Write access to the repo directory (storage/, log/, tests/, sql/ are written to).
- The venv is at `.venv` in the repo root; pip needs internet access to rebuild it.
- A full run takes about 40 seconds and makes about 108 requests, assuming no rate limiting (each 429 adds a 60s wait).

<!-- END OF LOCAL INSTRUCTIONS. Future Planning section is appended below. -->

## Future: Azure + Snowflake

Plan only; nothing here is implemented. The goal is to keep the pipeline's contracts (per country+indicator unit, `request_id`, `row_count` gate, PK upsert, lineage columns) and swap the storage and compute.

### Target architecture

| Local | Azure / Snowflake |
|---|---|
| `storage/landing`, `storage/archived` | ADLS Gen2 containers `landing/` and `archive/` (lifecycle rule moves old archive to cool tier) |
| `storage/staging/*.psv` | Snowflake external stage over ADLS, then a transient `STG_<indicator>` table via `COPY INTO` |
| `storage/data/*.psv` base tables | Snowflake base tables, loaded by `MERGE` on the config `primary_key` |
| `request_id_linage`, `staging_merged_id_linage` | Kept as `ARRAY` columns (append only if not present), plus `loaded_at` |
| `rejected/` | `rejected/` container, same stale-file rule |
| DuckDB `sql/code/analysis.sql` | Snowflake SQL, later a dbt model with the same view names |
| `config/pipeline.yaml` | Same YAML in the repo, read at job start; no code change to add a country or indicator |

### Ingestion
- One unit = one country+indicator pull. Run as an Azure Container Apps job (or Function) that writes JSON to `landing/` and logs `request_id`. ADF is an alternative if the team prefers low code.
- Fan out units in parallel with a small concurrency cap to respect World Bank rate limits.
- Keep the retry policy (429, 5xx, connection errors, backoff, max retries), but use exponential backoff with jitter instead of a fixed 60s sleep.
- Secrets (Snowflake key pair or OAuth, any API keys) live in Key Vault. Jobs use managed identity; no credentials in config or images.

### Orchestration and scheduling
- ADF triggers, or Airflow/Dagster if lineage and backfill UI matter. One DAG: ingest, schema check, stage, `row_count`, archive, `MERGE`, analysis refresh.
- Call the API source metadata (`lastupdated`) first and skip units whose value is unchanged since the last successful run. Store it in a small `PIPELINE_RUNS` table.
- Keep the exit-code semantics as task failure types so retries and alerts can tell API, schema, row_count and merge failures apart.

### Monitoring and alerting
- Structured logs to Log Analytics, keyed by `request_id` (one per pull) plus a run id for the whole DAG.
- Alerts (Azure Monitor action group to email/Teams) on: any failed run, `row_count` failure, no successful run within the expected window (freshness), and retry exhaustion.
- Write `row_count` results to a Snowflake table instead of report files so trends are queryable.

### Backfills
- Parameterize date range, country list and indicator list per run; defaults come from the YAML.
- `MERGE` on PK is idempotent, so reruns and overlapping ranges are safe; only lineage arrays grow.
- Replay from `archive/` without calling the API: re-stage the archived JSON for a given `request_id` and re-merge.

### Data quality
- Generalize `row_count` into a test registry run against staging before merge: row count, PK uniqueness, not-null on PK, value ranges. Same halt-before-merge behavior.
- Add schema contract tests (expected columns and types per indicator) at both the JSON check and the Snowflake table level. In dbt these are `tests:` plus `contract: enforced`.
- Failed units quarantine to `rejected/`; the other units continue.

### CI/CD and environments
- Separate dev, test and prod: one Snowflake database each, one resource group each, same Bicep/Terraform modules.
- GitHub Actions or Azure DevOps: run `pytest` and lint on PR, build the container, deploy infra and jobs to dev on merge, promote to prod with approval.
- Keep the unit tests network-free; add a small integration test against dev that pulls one country+indicator and merges it.
- Snowflake objects and dbt models are versioned in the repo and deployed by the pipeline, not by hand.

### Cost and security
- Use an XS warehouse with auto-suspend at 60s; data volume is tiny, so the cost is mostly orchestration and idle time. Use serverless jobs that scale to zero.
- Private endpoints for ADLS and Key Vault; Snowflake network policy; storage integration instead of keys.
- Least-privilege roles: a loader role (write stage and base tables) and a read-only analyst role.
- Archive retention set by policy; archived JSON is public data, but treat the pattern as if it were not.
