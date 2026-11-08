"""DuckDB analysis (plan section 13 and the Analysis row of section 15)."""
import duckdb
import numpy as np
import pytest

from helpers import (GDP, LE, analysis_out, base_path, psv_read, series_rows, write_base)
from src import analysis
from src.analysis import AnalysisError

R, S = "20261108T100000000001", "aaaaaaaa-0000-4000-8000-000000000001"
COLS = ["countryiso3code", "n_years", "first_year", "last_year", "avg_life_expectancy",
        "avg_gdp_per_capita", "corr_le_gdp_pc", "slope_years_per_10k_usd"]
YEARS = list(range(2018, 2025))
LE_S = {"BRA": [74.0, 74.2, 74.5, 72.0, 72.8, 75.0, 75.3],
        "CAN": [81.9, 82.0, 82.3, 81.7, 81.5, 82.4, 82.6]}
GDP_S = {"BRA": [9000.0, 9100.0, 8700.0, 7000.0, 7800.0, 9300.0, 10000.0],
         "CAN": [46000.0, 46400.0, 43500.0, 52000.0, 55500.0, 53000.0, 54000.0]}


def as_series(d, years=YEARS):
    return {c: dict(zip(years, vals)) for c, vals in d.items()}


def write_tables(root, le=None, gdp=None):
    write_base(root, LE, series_rows(le if le is not None else as_series(LE_S)), R, S)
    write_base(root, GDP, series_rows(gdp if gdp is not None else as_series(GDP_S)), R, S)


@pytest.fixture
def project(cfg, tmp_path, real_sql):
    write_tables(tmp_path)
    return cfg


def result(cfg, root):
    out = analysis.run_analysis(cfg)
    return out, psv_read(out)


def expected(le, gdp):
    le, gdp = np.array(le), np.array(gdp)
    return dict(n=len(le), avg_le=le.mean(), avg_gdp=gdp.mean(), corr=np.corrcoef(le, gdp)[0, 1],
                slope=np.polyfit(gdp, le, 1)[0] * 10000)


def check_row(row, exp, n=None):
    assert int(row["n_years"]) == (n or exp["n"])
    assert float(row["avg_life_expectancy"]) == pytest.approx(exp["avg_le"], abs=0.006)
    assert float(row["avg_gdp_per_capita"]) == pytest.approx(exp["avg_gdp"], abs=0.006)
    assert float(row["corr_le_gdp_pc"]) == pytest.approx(exp["corr"], abs=1.5e-4)
    assert float(row["slope_years_per_10k_usd"]) == pytest.approx(exp["slope"], abs=1.5e-4)


def test_output_path_header_and_format(project, tmp_path):
    out, df = result(project, tmp_path)
    assert out == analysis_out(tmp_path) == tmp_path / "sql" / "analysis" / "life_expectancy_by_gdp_per_capita.psv"
    text = out.read_text()
    assert text.splitlines()[0] == "|".join(COLS)
    assert all(l.count("|") == 7 for l in text.splitlines())
    assert "," not in text.splitlines()[0]
    assert list(df.columns) == COLS


def test_one_row_per_country_sorted(project, tmp_path):
    _, df = result(project, tmp_path)
    assert df["countryiso3code"].tolist() == ["BRA", "CAN"]


def test_metrics_match_independent_numpy_computation(project, tmp_path):
    _, df = result(project, tmp_path)
    by = df.set_index("countryiso3code")
    for c in ("BRA", "CAN"):
        check_row(by.loc[c], expected(LE_S[c], GDP_S[c]))
        assert by.loc[c, "first_year"] == "2018" and by.loc[c, "last_year"] == "2024"


def test_sql_file_exists_in_repository_and_is_static():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "sql" / "code" / "analysis.sql").read_text()
    sql = "\n".join(line.split("--")[0] for line in text.splitlines())  # ignore comments
    assert "$start_year" in sql and "$end_year" in sql
    for code in ("2018", "2024", "SP.DYN.LE00.IN", "NY.GDP.PCAP.CD"):
        assert code not in sql


def test_sql_file_from_config_is_the_one_executed(cfg_dict, load_cfg, tmp_path):
    write_tables(tmp_path)
    p = tmp_path / "queries" / "custom.sql"
    p.parent.mkdir()
    p.write_text("SELECT countryiso3code, $start_year AS s, $end_year AS e FROM life_expectancy "
                 "ORDER BY countryiso3code, date LIMIT 1;\n")
    cfg_dict["paths"]["analysis_sql"] = "queries/custom.sql"
    cfg_dict["analysis"]["output_file"] = "custom_out.psv"
    cfg = load_cfg(cfg_dict)
    out = analysis.run_analysis(cfg)
    assert out == tmp_path / "sql" / "analysis" / "custom_out.psv"
    assert out.read_text().splitlines() == ["countryiso3code|s|e", "BRA|2018|2024"]


def test_parameters_are_bound_from_config(cfg_dict, load_cfg, tmp_path):
    write_tables(tmp_path)
    p = tmp_path / "sql" / "code" / "analysis.sql"
    p.parent.mkdir(parents=True)
    p.write_text("SELECT $start_year AS s, $end_year AS e;\n")
    cfg_dict["date_range"] = {"start": 2019, "end": 2021}
    out = analysis.run_analysis(load_cfg(cfg_dict))
    assert out.read_text().splitlines() == ["s|e", "2019|2021"]


def test_tables_and_views_are_available_to_the_sql(cfg, tmp_path):
    write_tables(tmp_path)
    p = tmp_path / "sql" / "code" / "analysis.sql"
    p.parent.mkdir(parents=True)
    p.write_text("SELECT (SELECT COUNT(*) FROM life_expectancy_years) AS a,"
                 " (SELECT COUNT(*) FROM gdp_per_capita_current_usd) AS b,"
                 " (SELECT COUNT(*) FROM life_expectancy) AS c,"
                 " (SELECT COUNT(*) FROM gdp_per_capita) AS d WHERE $start_year <= $end_year;\n")
    out = analysis.run_analysis(cfg)
    assert out.read_text().splitlines() == ["a|b|c|d", "14|14|14|14"]


def test_date_filter_honors_config_range(cfg_dict, load_cfg, tmp_path, real_sql):
    write_tables(tmp_path)
    cfg_dict["date_range"] = {"start": 2019, "end": 2020}
    cfg = load_cfg(cfg_dict)
    _, df = result(cfg, tmp_path)
    by = df.set_index("countryiso3code")
    for c in ("BRA", "CAN"):
        check_row(by.loc[c], expected(LE_S[c][1:3], GDP_S[c][1:3]))
        assert by.loc[c, "first_year"] == "2019" and by.loc[c, "last_year"] == "2020"


def test_rows_outside_the_range_are_excluded(cfg, tmp_path, real_sql):
    le = {"BRA": dict(zip(range(2016, 2027), [60 + i for i in range(11)]))}
    gdp = {"BRA": dict(zip(range(2016, 2027), [1000.0 * (i + 1) ** 2 for i in range(11)]))}
    write_tables(tmp_path, le, gdp)
    _, df = result(cfg, tmp_path)
    assert df["n_years"].tolist() == ["7"] and df["first_year"].tolist() == ["2018"]
    assert df["last_year"].tolist() == ["2024"]


def test_null_values_are_excluded_from_n_years_and_averages(cfg, tmp_path, real_sql):
    le = as_series(LE_S)
    le["BRA"][2020] = ""      # empty cell = null value in the base file
    gdp = as_series(GDP_S)
    gdp["CAN"][2022] = ""
    write_tables(tmp_path, le, gdp)
    _, df = result(cfg, tmp_path)
    by = df.set_index("countryiso3code")
    keep_b = [i for i, y in enumerate(YEARS) if y != 2020]
    keep_c = [i for i, y in enumerate(YEARS) if y != 2022]
    check_row(by.loc["BRA"], expected([LE_S["BRA"][i] for i in keep_b], [GDP_S["BRA"][i] for i in keep_b]))
    check_row(by.loc["CAN"], expected([LE_S["CAN"][i] for i in keep_c], [GDP_S["CAN"][i] for i in keep_c]))
    assert by.loc["BRA", "n_years"] == "6" and by.loc["CAN", "n_years"] == "6"


def test_only_matching_country_years_are_joined(cfg, tmp_path, real_sql):
    le = as_series(LE_S)
    gdp = as_series(GDP_S)
    del gdp["BRA"][2018]          # BRA has no GDP per capita for 2018 -> 6 joined years
    write_tables(tmp_path, le, gdp)
    _, df = result(cfg, tmp_path)
    by = df.set_index("countryiso3code")
    assert by.loc["BRA", "n_years"] == "6" and by.loc["BRA", "first_year"] == "2019"
    assert by.loc["CAN", "n_years"] == "7"


def test_country_present_in_only_one_table_yields_no_row(cfg, tmp_path, real_sql):
    le = as_series(LE_S)
    le["DEU"] = {y: 80.0 + i * 0.1 for i, y in enumerate(YEARS)}
    write_tables(tmp_path, le, None)
    _, df = result(cfg, tmp_path)
    assert df["countryiso3code"].tolist() == ["BRA", "CAN"]


def test_constant_life_expectancy_gives_empty_corr_never_nan(cfg, tmp_path, real_sql):
    le = as_series(LE_S)
    le["CAN"] = {y: 82.0 for y in YEARS}
    write_tables(tmp_path, le, None)
    out, df = result(cfg, tmp_path)
    by = df.set_index("countryiso3code")
    assert by.loc["CAN", "corr_le_gdp_pc"] == ""
    assert "nan" not in out.read_text().lower()
    assert float(by.loc["CAN", "avg_life_expectancy"]) == 82.0
    assert float(by.loc["BRA", "corr_le_gdp_pc"]) == pytest.approx(expected(LE_S["BRA"], GDP_S["BRA"])["corr"], abs=1.5e-4)


def test_single_year_gives_empty_corr(cfg, tmp_path, real_sql):
    le = {"BRA": {2020: 74.0}}
    gdp = {"BRA": {2020: 9000.0}}
    write_tables(tmp_path, le, gdp)
    out, df = result(cfg, tmp_path)
    assert df["n_years"].tolist() == ["1"] and df["corr_le_gdp_pc"].tolist() == [""]
    assert "nan" not in out.read_text().lower()


def test_extra_base_columns_do_not_change_the_result(cfg, tmp_path, real_sql):
    write_tables(tmp_path)
    out, with_14 = result(cfg, tmp_path)
    text14 = out.read_text()
    # rewrite both base files with only the three columns the SQL uses
    for ind in (LE, GDP):
        df = psv_read(base_path(tmp_path, ind))[["countryiso3code", "date", "value"]]
        df.to_csv(base_path(tmp_path, ind), sep="|", index=False, lineterminator="\n")
    out3 = analysis.run_analysis(cfg)
    assert out3.read_text() == text14


def test_rerun_overwrites_output_with_identical_bytes(cfg, tmp_path, real_sql):
    write_tables(tmp_path)
    first = analysis.run_analysis(cfg).read_bytes()
    assert analysis.run_analysis(cfg).read_bytes() == first


# ----------------------------------------------------------------------------- load_base_tables
def test_load_base_tables_creates_tables_with_all_columns_as_varchar_and_alias_views(cfg, tmp_path):
    write_tables(tmp_path)
    con = duckdb.connect()
    try:
        analysis.load_base_tables(con, cfg)
        for name in ("life_expectancy_years", "gdp_per_capita_current_usd", "life_expectancy", "gdp_per_capita"):
            desc = con.execute(f"DESCRIBE SELECT * FROM {name}").fetchall()
            assert len(desc) == 14, name
            assert all(row[1] == "VARCHAR" for row in desc), name
        assert con.execute("SELECT COUNT(*) FROM life_expectancy").fetchone()[0] == 14
        names = [r[0] for r in con.execute("DESCRIBE SELECT * FROM life_expectancy").fetchall()]
        assert names[:2] == ["countryiso3code", "date"] and names[-2:] == ["request_id_linage", "staging_merged_id_linage"]
    finally:
        con.close()


def test_load_base_tables_follows_config_table_names(cfg_dict, load_cfg, tmp_path):
    cfg_dict["indicators"][0]["table_name"] = "le_custom"
    cfg = load_cfg(cfg_dict)
    write_tables(tmp_path)
    con = duckdb.connect()
    try:
        analysis.load_base_tables(con, cfg)
        assert con.execute("SELECT COUNT(*) FROM le_custom").fetchone()[0] == 14
        assert con.execute("SELECT COUNT(*) FROM life_expectancy").fetchone()[0] == 14
    finally:
        con.close()


# ----------------------------------------------------------------------------- errors
def test_missing_base_file_raises_analysis_error(cfg, tmp_path, real_sql):
    write_base(tmp_path, LE, series_rows(as_series(LE_S)), R, S)  # GDP base missing
    with pytest.raises(AnalysisError) as ei:
        analysis.run_analysis(cfg)
    assert GDP in str(ei.value) or "gdp" in str(ei.value).lower()


def test_missing_sql_file_raises_analysis_error(cfg, tmp_path):
    write_tables(tmp_path)
    with pytest.raises(AnalysisError):
        analysis.run_analysis(cfg)


def test_invalid_sql_raises_analysis_error_wrapping_duckdb(cfg, tmp_path):
    write_tables(tmp_path)
    p = tmp_path / "sql" / "code" / "analysis.sql"
    p.parent.mkdir(parents=True)
    p.write_text("SELEC nothing FROM nowhere;\n")
    with pytest.raises(AnalysisError):
        analysis.run_analysis(cfg)
    p.write_text("SELECT * FROM table_that_does_not_exist;\n")
    with pytest.raises(AnalysisError):
        analysis.run_analysis(cfg)


def test_analysis_error_is_a_pipeline_error():
    assert "PipelineError" in [c.__name__ for c in AnalysisError.__mro__]
