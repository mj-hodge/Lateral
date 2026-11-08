# World Bank Indicator Pipeline: Phased Multi-Agent Build

You are the orchestrator of a phased, multi-agent build. Run the phases in order and stop at each approval gate.

## Goal

Build a Python data pipeline that pulls World Bank API data, lands it as JSON, validates it, stages it, merges it idempotently into base tables, and runs a SQL analysis in DuckDB.

- **Countries (ISO3):** `BRA, CAN, DEU, GBR, IND, JPN, MEX, USA, ZAF`
- **Indicators:**

| Name | Code |
|---|---|
| Population, total | `SP.POP.TOTL` |
| GDP, current US dollars | `NY.GDP.MKTP.CD` |
| GDP per capita, current US dollars | `NY.GDP.PCAP.CD` |
| Life expectancy at birth, total years | `SP.DYN.LE00.IN` |

- **Date range:** 2018 to 2024 inclusive.
- **API docs:** https://datahelpdesk.worldbank.org/knowledgebase/articles/898581-api-basic-call-structures. Use them to build the request URLs dynamically from config.

## Environment and constraints

- Available packages: `requests`, `pandas`, `duckdb`, plus a virtual environment. `pip install` anything else you need and list it in `requirements.txt`.
- Every file and directory you create must be inside `/home/hodge/Lateral`.
- Procedural style is fine. Use small functions (for example `fetch_page`, `fetch_country`, `schema_check`) and loop over them. No classes are required.
- The API does not guarantee result order, so any failure restarts that pull. Keep pulls small, **one request series per country and indicator**, never one large pull. That way a failure only reprocesses a small unit.
- All dates, paths, country codes and indicator codes come from config. Nothing is hardcoded.

## API example

URL: `https://api.worldbank.org/v2/country/BRA/indicator/SP.POP.TOTL?format=json&date=2018:2020&page=1`

Response (a 2-element JSON array: page metadata, then records):

```json
[
  {"page": 1, "pages": 1, "per_page": 50, "total": 1, "sourceid": "2", "lastupdated": "2026-10-08"},
  [
    {
      "indicator": {"id": "SP.POP.TOTL", "value": "Population, total"},
      "country": {"id": "BR", "value": "Brazil"},
      "countryiso3code": "BRA",
      "date": "2025",
      "value": 212812405,
      "unit": "",
      "obs_status": "",
      "decimal": 0
    }
  ]
]
```

## Data context

All four indicators share the same primary key, `[countryiso3code, date]`. Each row is one country-year value.

| Indicator | Row meaning |
|---|---|
| `SP.POP.TOTL` | Total population for a country in a year |
| `NY.GDP.MKTP.CD` | GDP in current USD for a country in a year |
| `NY.GDP.PCAP.CD` | GDP per capita in current USD for a country in a year |
| `SP.DYN.LE00.IN` | Life expectancy at birth (years) for a country in a year |

## Requirements

**Config**
- A YAML config holds the countries, the indicators (code and name), the date range, the base paths and the retry settings.
- Adding a country or indicator is a YAML-only change with no code changes. The primary key is also per-indicator config.

**Ingestion**
- Request format is JSON (`format=json`).
- Page through results using `pages` from the page-1 metadata until all pages are fetched.
- Each country and indicator pull gets one `request_id`, the current timestamp (for example `20261108T153045123456`). Every page of that pull shares the same `request_id`.
- **Retry on 429:** sleep 60 seconds, then retry. After 3 retries, fail the pipeline with a clear error.
- Landing file name is `<country-code>_<indicator-code>_<request_id>.json`, in `storage/landing/<indicator-code>/`.
- There is one landing file per country per indicator. Because the name has no page number, write all pages of a pull into that one file as a list of raw page responses. State this assumption in the plan.
- The first step after the API returns is a **schema check**. It validates the metadata keys, the record keys and types, `countryiso3code`, `date` and `indicator.id`. A failure stops that pull and fails loudly. It does not silently drop data.

**Request log**
- On page 1 of every pull, record `total` and `pages`.
- Write one log file per request, `log/<indicator-code>/<request_id>.log`.
- The line is pipe-delimited: `request_id|total|pages|country_code|indicator_code`.

**Staging and tests**
- Read all landing JSON files for an indicator into pandas. Keep a per-country dataframe first, then combine them into one staging dataframe.
- Write the staging dataframe as a pipe-delimited file, `storage/staging/<indicator-code>/...psv`.
- Generate a `staging_merge_id` (random UUID) when the staging table is created, and add it as a column.
- **Data quality test, `row_count` (the only test for now):**
  - For each `request_id`, compare the `total` in its log with the row count of that country's pandas dataframe, taken before the dataframes are combined.
  - Write a pass/fail report per `request_id` to `tests/<indicator-code>/row_count/`.
  - Define what happens to the run on a failure (recommended: stop before archive and merge, and leave landing files in place for rerun).

**Archive and merge**
- After staging and tests pass, move the landing files to `storage/archived/<indicator-code>/` so that landing is empty.
- Read the staging file and the existing base table, `storage/data/<indicator-code>/`, into pandas. Merge on the primary key (upsert: new keys are inserted, existing keys are updated). Reruns must be idempotent.
- Add two list-valued columns to the base table:
  - `request_id_linage`: appends the latest `request_id` for that row.
  - `staging_merged_id_linage`: appends the latest `staging_merge_id` for that row.
- Reruns with identical data change only these lineage lists, never the business values. Do not append the same id twice.
- Write the base table back to the same location, overwriting it, as a pipe-delimited file.
- Serialize the list columns in a way that can be read back and appended to on the next run (for example JSON or a `;`-separated string, and decide which one in the plan). Pipes must not appear inside a field.
- If the base table does not exist yet, create it from staging.

**SQL analysis**
- Load all four base files into DuckDB.
- Write a SQL query that analyzes **life expectancy by GDP per capita for each country**. Join on `countryiso3code` and `date`. Output per country, for example average life expectancy, average GDP per capita and the correlation between them across 2018 to 2024. State which metrics you chose and why.
- Write the result as a pipe-delimited file in `sql/analysis/`.
- Save the query itself to `sql/code/analysis.sql`.

## Suggested layout

```
config/pipeline.yaml
src/              # ingest, schema check, staging, tests, merge, analysis, run_pipeline
storage/{landing,staging,archived,data}/<indicator-code>/
log/<indicator-code>/<request_id>.log
tests/<indicator-code>/row_count/   # reports
tests/                              # pytest unit tests
sql/analysis/  sql/code/analysis.sql
plan/plan.html
README.md
```

The planning agent may adjust this layout, but it must keep every path required above.

## Process

Agents are separated to keep the checks honest.

**Anti-collusion rules**
- The Testing agent writes tests from the requirements and the approved plan only. It never sees the implementation.
- The Coding agent must not edit tests to make them pass. If a test looks wrong, it reports that to the Code Review agent and the orchestrator.
- The Code Review agent checks the code against the plan, the requirements and the tests. It also checks that tests are not weakened, skipped or hardcoded around.
- Reviewers return findings. They do not fix the work they are reviewing.

**Agent profiles**

| Agent | Role |
|---|---|
| Planning | Designs the architecture, pipeline and process flow from all of the above |
| Plan Review | Reviews the plan against the requirements and the transformation flow below |
| Testing | Designs and writes tests against the requirements and the plan |
| Coding | Implements the code |
| Code Review | Reviews the code against the plan, requirements and tests |
| Instructions | Writes the local run instructions |
| Future Planning | Plans the Azure and Snowflake version |

**Transformation flow (reference for the Plan Review and Code Review agents)**
1. Pull each country's data for an indicator from the API and land it as JSON, one file per country.
2. Schema check, then read all landing JSON into pandas and write the pipe-delimited staging file.
3. Run the `row_count` test.
4. Move landing files to archived.
5. Merge staging into the base table on the primary key, updating the lineage columns.
6. Load the base tables into DuckDB, run the analysis SQL and write the output.

### Phase 1: Plan
1. The Planning agent produces the design.
2. The Plan Review agent checks it against the requirements and the transformation flow. If it finds issues or gaps, it sends the plan back to the Planning agent. Repeat until the review passes.
3. Write the plan as an interactive HTML file at `plan/plan.html`. It should have a navigable layout covering the architecture, the data flow, the config schema, the folder layout, the failure and retry behavior, the idempotency approach and the test strategy. It must list the assumptions and open questions, including the one-file-per-country and `row_count` failure decisions above.
4. **Stop and wait for human approval.**

### Phase 2: Tests
- The Testing agent writes tests from the approved plan. Cover at least:
  - URL building from config
  - 429 retry (3 retries, then fail)
  - paging
  - schema check pass and fail
  - request_id shared across pages
  - file and log naming and format
  - `row_count` pass and fail
  - merge idempotency (running twice gives the same business data, with the lineage lists growing once per run)
  - new-key insert and existing-key update
  - lineage list handling
  - no secret or network dependence (mock the API)

### Phase 3: Code
1. The Coding agent implements the pipeline.
2. The Code Review agent reviews the code against the plan, requirements and tests, and runs the tests. If it finds issues or gaps, it sends the work back to the Coding agent. Repeat until the review passes.
3. Run the pipeline end to end against the real API once, and report the results.
4. **Stop and wait for human approval.**

### Phase 4: Documentation
- **Instructions agent:** write the local run steps in the root `README.md`.
  - Cover setup (the venv currently holds the needed packages), the commands in order, and where to find the outputs, including the final SQL analysis.
  - Call out the assumptions about someone else's machine: Python version, OS and shell, network access to `api.worldbank.org`, the venv location and how to activate it, and the working directory.
- **Future Planning agent:** add a short, concise section to the root `README.md` on moving this workflow to Azure with Snowflake as the warehouse. Cover ingestion, scheduling, monitoring, backfills, idempotency, secrets and cost. Keep it brief.

### Phase 5: Ship
- Commit the changes and push them. Check `git status` first and make sure nothing sensitive or generated is staged (venv, large data files). Use a clear commit message.

## Reporting

At each gate, give a short summary of what was done and any open questions. Do not continue past a gate without human approval.
