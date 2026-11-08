You are not doing changes, I need help with writing the prompt that will do the changes.

Goal:
You are a expert python developer that is tasked with building a data pipeline from an api.
    - County Codes in scope
        - [BRA, CAN, DEU, GBR, IND, JPN, MEX, USA, ZAF ]
    - Indicator Codes in Scope
        - [{"Population, total":"SP.POP.TOTL"}, {"GDP, current US dollars":"NY.GDP.MKTP.CD"},{"GDP per capita, current US dollars":"NY.GDP.PCAP.CD"},{"Life expectancy at birth, total years":"SP.DYN.LE00.IN"}]
    The pipleine needs to pull data from 2018 to 2024 for these country codes and these Indicator codes.

Context:
    This is the API documentaion `https://datahelpdesk.worldbank.org/knowledgebase/articles/898581-api-basic-call-structures` use this to figure out the How to dynamically structure the API url.

    Since the API can't guarentee order of items returned, any failure would result in restarting the data pull. Large all incompassing data pulls should be avoided to prevent large reprocessing during failures.

    Python packages you have avaialable requests, pandas, duckdb. If any others are need please pip install them

Exmaples:
    - API url: https://api.worldbank.org/v2/country/BRA/indicator/SP.POP.TOTL?format=json&date=2018:2020&page=1
        - this example use `BRA` country code and `SP.POP.TOTL` as indicator code with a json format for output and a date range of 2018 to 2020 and what page is being pulled
    - Json Output: [
    {
        "page": 1,
        "pages": 1,
        "per_page": 50,
        "total": 1,
        "sourceid": "2",
        "lastupdated": "2026-10-08"
    },
    [
        {
            "indicator": {
                "id": "SP.POP.TOTL",
                "value": "Population, total"
            },
            "country": {
                "id": "BR",
                "value": "Brazil"
            },
            "countryiso3code": "BRA",
            "date": "2025",
            "value": 212812405,
            "unit": "",
            "obs_status": "",
            "decimal": 0
        }
    ]
]

Requirements for data ingestion:
    - api response should be json format
    - Use Yaml config design for country codes and indicator codes
    - Coding style, I'm ok with a more procedural code style instead of Object Oriented Programming. You can create funtions for say API calls and loop through that function to pull data
    - if you hit 429 response error on the API sleep for 60 seconds and then retry then fail the pipline after 3 retries
    - for each request I would like to add a request_id to the output and that request_id should be the current timestamp.
    - Output file name should be `<country-code>_<indicator-code>_<request_id>.json`
    - file structure for the output should be storage/landing/<indicator-code> for the data pull. The code should clear the folder by moving data to an archived folder storage/archived/<indicator-code> once data has been processed into the staging folder storage/staging/<indicator-code>
    - file structure for the merged data should be storage/data/<indicator-code>. data should be merged based on Primary key. This keeps the pipeline idempotent.
    - The code should handle paging through the api. the page count is represented in the output by `pages` and each outfile from the paged calls should have the same request_id
    - I should be able to add a new country or indicator code by adding it to the configs with no code changes 
    - first step in after api return files should be a schema check
    - for each request on the page 1 record the `total` and keep a log of the request_ids. should be stored in `log/<indicator-code>`. log file should be named `<request_id>.log` log should contain the request_id|`total`|`pages`|country code|indicator code and make it pipe delimited
    - Data quality test should be kept in tests/<indicator-code>/<test_type>. There is only 1 current test i want implemented and thats a `row_count`. row_count test should happen against the log for that request_id and `total` the pandas dataframe that turn the landing json into a dataframe before its combined into the staging dataframe that includes all countries. There should be a report on tests passing or failing in that test location by request_id
    - A staging_merge_id should be created once the staging table is created and that can be a random uuid. This will keep track of each merge to the base dataset
    - all file and directory creation should happen within Lateral folder



Data Context:
    - SP.POP.TOTL (Population,total)
        - Primary key: [countryiso3code,date]
        - Description: each row should represent a pouplation total per year for a given country
    - NY.GDP.MKTP.CD (GDP, current US dollars)
        - Primary key: [countryiso3code,date]
        - Description: each row should represent a the GDP in USD dollars for a given country in a given year
    - NY.GDP.PCAP.CD (GDP per capita, current US dollars)
        - Primary key: [countryiso3code,date]
        - Description: each row should represent GDP per capita in USD dollars for a given country in a given year
    - SP.DYN.LE00.IN (Life expectancy at birth, total years )
        - Primary key: [countryiso3code,date]
        - Description: each row should represent Life expectancy at birth for a given country in a given year   

Transformation flow:

    1. code pulls all country code Data for the indicator code and lands from api in json files. There should be 1 file per country.
    2. All indicator Json file ingested into pandas dataframe and write pipe `|` delimited psv file to storage/staging/<indicator-code> 
    3. this is where row_count test happen
    4. Move files from storage/landing/<indicator-code> to storage/archived/<indicator-code>
    5. pipe demilited file from step 2 should then be merged with the existing base table in storage/data/<indicator-code>. Both the staging file and the base file should be read into pandas data frames and then merged based on Primary Key. This make operations idempotent. This would cause the request_id to change when a rerun happens and the same data comes in, so give me an extra column in the base table that holds and keeps appending the latest request_id and call it request_id_linage. make request_id_linage a list that holds the request_id. do the same thing to keep track of the staging_merge_id and create staging_merged_id_linage. This base table should be written back to the same location overwitting the current table. it should be a `|` pipe delimited file
    6. I need all these base files ingested into duckdb and I need a sql query that does analyis on   `Life expectancy at birth, total years` by `GDP per capita` for each country and out puts that as a pipe delimited file at this location sql/analysis and outputs the sql query itself to sql/code/analysis.sql


Process:
I want this to happen in a phased multi agent process.
I dont want agents to collude to make tests pass.

    - agent profiles:
        - Planning agent: plans code design using all the context and examples
        - Plan Review agent: reviews plans against requirements and Transformation Flow 
        - Coding agent: Actually creates the code
        - Code Review agent: reviews the code against the Plan and requirements and the tests
        - Testing agent: Designs tests against the requirements and Plan.
        - Instructions agent: give instruction on how someone would run this repo locally. it currently holds a virutal env with the needed packages, but call out any assumptions made to the another person's local setup for these instructions to work. the instruction should launch the python scripts required to call all the data needed and get the out put of the sql anaysis at the end. put those instructions in the README.md
        - Future Planning Agent: Will plan how they design this local python workflow for the azure cloud and Snowflake as a data warehouse. How you you handle ingestion, scheduling, monitoring, backfills, etcs. and he put this informationin the roots README.md. please keep it short succint. 
Phase 1
    - planning agent uses context to design an architecture, pipeline, process flow that accomplishes the goal
    - Plan review agent reviews plan, if issues or gaps are found return to planning agent for corrections
    - plan should be output in interactive html file at this location plan/plan.html
    - then wait for approval
Phase 2
    - Testing agent create tests
Phase 3
    - Coding agent implements code changes
    - Code review agent reviews and checks tests, if there are issues or gaps it is passed back to coding agent
    - Then wait for human approval
Phase 4
    - Instruction agent run
    - future planning agent run
phase 5
    - commit and push changes


output this prompt to `/home/hodge/Lateral/Prompt/prompt.md`





