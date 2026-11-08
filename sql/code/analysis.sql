-- Life expectancy by GDP per capita, per country, for the configured date range.
-- Tables/views expected: life_expectancy, gdp_per_capita
--   (columns: countryiso3code, date, value, ... all VARCHAR, as loaded from the base .psv files)
-- Parameters: $start_year, $end_year (bound by src/analysis.py from config/pipeline.yaml)
--
-- Metrics (one row per country):
--   n_years                  country-years with both values (guards how far the other metrics can be trusted)
--   first_year / last_year   the span actually covered
--   avg_life_expectancy      level summary of life expectancy over the window
--   avg_gdp_per_capita       level summary of GDP per capita (current US$) over the window
--   corr_le_gdp_pc           Pearson correlation of the yearly pairs: the direct "life expectancy by GDP
--                            per capita" relationship; empty when a series is constant or n < 2
--   slope_years_per_10k_usd  REGR_SLOPE * 10000: years of life expectancy per US$10,000, the effect size
--                            that a correlation alone hides
WITH joined AS (
    SELECT
        le.countryiso3code,
        CAST(le.date AS INTEGER)           AS year,
        TRY_CAST(le.value AS DOUBLE)       AS life_expectancy,
        TRY_CAST(g.value  AS DOUBLE)       AS gdp_per_capita
    FROM life_expectancy AS le
    INNER JOIN gdp_per_capita AS g
        ON  le.countryiso3code = g.countryiso3code
        AND le.date            = g.date
    WHERE CAST(le.date AS INTEGER) BETWEEN $start_year AND $end_year
),
complete AS (
    SELECT * FROM joined
    WHERE life_expectancy IS NOT NULL AND gdp_per_capita IS NOT NULL
)
SELECT
    countryiso3code,
    COUNT(*)                                              AS n_years,
    MIN(year)                                             AS first_year,
    MAX(year)                                             AS last_year,
    ROUND(AVG(life_expectancy), 2)                        AS avg_life_expectancy,
    ROUND(AVG(gdp_per_capita), 2)                         AS avg_gdp_per_capita,
    -- CORR on a constant series can yield NaN; normalize NaN to NULL (empty cell)
    CASE WHEN isnan(CORR(life_expectancy, gdp_per_capita)) THEN NULL
         ELSE ROUND(CORR(life_expectancy, gdp_per_capita), 4) END   AS corr_le_gdp_pc,
    CASE WHEN isnan(REGR_SLOPE(life_expectancy, gdp_per_capita)) THEN NULL
         ELSE ROUND(REGR_SLOPE(life_expectancy, gdp_per_capita) * 10000, 4) END
                                                          AS slope_years_per_10k_usd
FROM complete
GROUP BY countryiso3code
ORDER BY countryiso3code;
