"""Config schema, validation, resolve_path, get_indicator (plan sections 3 and 5)."""
import copy
import re
from pathlib import Path

import pytest

from helpers import GDP, LE, POP, base_cfg_dict, write_cfg
from src.config import ConfigError, get_indicator, load_config, resolve_path


def _load(tmp_path, d, rel="config/pipeline.yaml"):
    return load_config(write_cfg(tmp_path, d, rel))


# ----------------------------------------------------------------------------- happy path
def test_load_valid_config(cfg, tmp_path):
    assert cfg["countries"] == ["BRA", "CAN"]
    assert cfg["date_range"]["start"] == 2018 and cfg["date_range"]["end"] == 2024
    assert [i["code"] for i in cfg["indicators"]] == [LE, GDP]
    assert cfg["retry"]["sleep_seconds"] == 60 and cfg["retry"]["max_retries"] == 3
    assert cfg["api"]["per_page"] == 3
    assert isinstance(cfg["_root"], Path)
    assert cfg["_root"].resolve() == tmp_path.resolve()


def test_root_is_parent_of_config_directory(tmp_path):
    d = base_cfg_dict()
    cfg = _load(tmp_path, d, rel="alt_conf/p.yaml")
    assert cfg["_root"].resolve() == tmp_path.resolve()
    cfg = _load(tmp_path, d, rel="a/b/c.yaml")
    assert cfg["_root"].resolve() == (tmp_path / "a").resolve()


def test_root_independent_of_cwd(tmp_path, monkeypatch):
    path = write_cfg(tmp_path, base_cfg_dict())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = load_config(path)
    assert cfg["_root"].resolve() == tmp_path.resolve()
    assert resolve_path(cfg, "landing", LE).resolve() == (tmp_path / "storage/landing" / LE).resolve()


def test_adding_country_and_indicator_is_yaml_only(tmp_path):
    d = base_cfg_dict()
    d["countries"].append("ZAF")
    d["indicators"].append({"code": POP, "name": "Population, total", "table_name": "population_total",
                            "primary_key": ["countryiso3code", "date", "unit"]})
    cfg = _load(tmp_path, d)
    assert cfg["countries"][-1] == "ZAF"
    assert get_indicator(cfg, POP)["primary_key"] == ["countryiso3code", "date", "unit"]
    assert get_indicator(cfg, POP)["table_name"] == "population_total"


@pytest.mark.parametrize("per_page, expected", [(50, 50), ("50", 50), ("3", 3), (None, None)])
def test_per_page_accepted_and_normalized(tmp_path, per_page, expected):
    d = base_cfg_dict()
    d["api"]["per_page"] = per_page
    cfg = _load(tmp_path, d)
    assert cfg["api"]["per_page"] == expected
    if expected is not None:
        assert type(cfg["api"]["per_page"]) is int


def test_per_page_may_be_omitted(tmp_path):
    d = base_cfg_dict()
    del d["api"]["per_page"]
    load_config(write_cfg(tmp_path, d))


def test_default_request_id_format_accepted(tmp_path):
    for fmt in ["%Y%m%dT%H%M%S%f", "%Y%m%d%H%M%S%f", "%Y%m%d-%H-%M-%S-%f"]:
        d = base_cfg_dict()
        d["request_id"]["format"] = fmt
        _load(tmp_path, d)


# ----------------------------------------------------------------------------- resolve_path / get_indicator
def test_resolve_path_fills_template_and_is_absolute_inside_root(cfg, tmp_path):
    p = resolve_path(cfg, "landing", LE)
    assert p.is_absolute()
    assert p == (cfg["_root"] / "storage" / "landing" / LE)
    assert resolve_path(cfg, "data", GDP) == cfg["_root"] / "storage" / "data" / GDP
    assert resolve_path(cfg, "request_log", LE) == cfg["_root"] / "log" / LE
    assert resolve_path(cfg, "row_count_reports", LE) == cfg["_root"] / "tests" / LE / "row_count"
    assert resolve_path(cfg, "staging_failed_marker", LE) == (
        cfg["_root"] / "storage" / "staging" / LE / "staging_failed_request_ids.txt")
    assert resolve_path(cfg, "analysis_sql") == cfg["_root"] / "sql" / "code" / "analysis.sql"
    assert resolve_path(cfg, "analysis_out") == cfg["_root"] / "sql" / "analysis"
    assert resolve_path(cfg, "run_log") == cfg["_root"] / "log" / "pipeline.log"


def test_resolve_path_does_not_create_anything(cfg, tmp_path):
    p = resolve_path(cfg, "landing", LE)
    assert not p.exists()
    assert not (tmp_path / "storage").exists()


def test_resolve_path_follows_config_change(tmp_path):
    d = base_cfg_dict()
    d["paths"]["landing"] = "somewhere/else/{indicator}/in"
    cfg = _load(tmp_path, d)
    assert resolve_path(cfg, "landing", LE) == cfg["_root"] / "somewhere" / "else" / LE / "in"


def test_get_indicator(cfg):
    ind = get_indicator(cfg, LE)
    assert ind["code"] == LE
    assert ind["name"] == "Life expectancy at birth, total years"
    assert ind["table_name"] == "life_expectancy_years"
    assert ind["primary_key"] == ["countryiso3code", "date"]


def test_get_indicator_unknown_raises(cfg):
    with pytest.raises(ConfigError):
        get_indicator(cfg, "XX.UNKNOWN")


# ----------------------------------------------------------------------------- invalid configs -> ConfigError
def _mut(fn):
    def apply(d):
        fn(d)
        return d
    return apply


INVALID = {
    "missing_api": _mut(lambda d: d.pop("api")),
    "missing_date_range": _mut(lambda d: d.pop("date_range")),
    "missing_countries": _mut(lambda d: d.pop("countries")),
    "missing_indicators": _mut(lambda d: d.pop("indicators")),
    "missing_retry": _mut(lambda d: d.pop("retry")),
    "missing_paths": _mut(lambda d: d.pop("paths")),
    "missing_request_id": _mut(lambda d: d.pop("request_id")),
    "missing_analysis": _mut(lambda d: d.pop("analysis")),
    "missing_path_key": _mut(lambda d: d["paths"].pop("landing")),
    "missing_indicator_table_name": _mut(lambda d: d["indicators"][0].pop("table_name")),
    "missing_indicator_primary_key": _mut(lambda d: d["indicators"][0].pop("primary_key")),
    "missing_indicator_name": _mut(lambda d: d["indicators"][0].pop("name")),
    "missing_indicator_code": _mut(lambda d: d["indicators"][0].pop("code")),
    "empty_countries": _mut(lambda d: d.__setitem__("countries", [])),
    "empty_indicators": _mut(lambda d: d.__setitem__("indicators", [])),
    "duplicate_country": _mut(lambda d: d["countries"].append("BRA")),
    "duplicate_indicator": _mut(lambda d: d["indicators"].append(copy.deepcopy(d["indicators"][0]))),
    "start_after_end": _mut(lambda d: d["date_range"].update(start=2025, end=2024)),
    "lowercase_country": _mut(lambda d: d["countries"].__setitem__(0, "bra")),
    "iso2_country": _mut(lambda d: d["countries"].__setitem__(0, "BR")),
    "four_letter_country": _mut(lambda d: d["countries"].__setitem__(0, "BRAZ")),
    "path_template_without_country": _mut(lambda d: d["api"].update(path_template="country/x/indicator/{indicator}")),
    "path_template_without_indicator": _mut(lambda d: d["api"].update(path_template="country/{country}/indicator/x")),
    "per_page_zero": _mut(lambda d: d["api"].update(per_page=0)),
    "per_page_negative": _mut(lambda d: d["api"].update(per_page=-3)),
    "per_page_non_digit_string": _mut(lambda d: d["api"].update(per_page="abc")),
    "per_page_float": _mut(lambda d: d["api"].update(per_page=2.5)),
    "path_escapes_root_dotdot": _mut(lambda d: d["paths"].update(landing="../outside/{indicator}")),
    "path_escapes_root_absolute": _mut(lambda d: d["paths"].update(data="/tmp/elsewhere/{indicator}")),
    "path_escapes_root_deep": _mut(lambda d: d["paths"].update(analysis_out="sql/../../out")),
    "request_id_without_microseconds": _mut(lambda d: d["request_id"].update(format="%Y%m%dT%H%M%S")),
    "request_id_with_underscore": _mut(lambda d: d["request_id"].update(format="%Y%m%d_%H%M%S%f")),
    "request_id_with_semicolon": _mut(lambda d: d["request_id"].update(format="%Y%m%d;%H%M%S%f")),
    "request_id_with_pipe": _mut(lambda d: d["request_id"].update(format="%Y%m%d|%H%M%S%f")),
    "request_id_with_slash": _mut(lambda d: d["request_id"].update(format="%Y%m%d/%H%M%S%f")),
    "request_id_with_space": _mut(lambda d: d["request_id"].update(format="%Y%m%d %H%M%S%f")),
    "request_id_not_starting_ymd": _mut(lambda d: d["request_id"].update(format="%H%M%S%f%Y%m%d")),
    "request_id_day_first": _mut(lambda d: d["request_id"].update(format="%d%m%YT%H%M%S%f")),
    "request_id_fields_out_of_order": _mut(lambda d: d["request_id"].update(format="%Y%m%dT%M%H%S%f")),
    "pk_contains_request_id": _mut(lambda d: d["indicators"][0].update(primary_key=["countryiso3code", "request_id"])),
    "pk_contains_staging_merge_id": _mut(lambda d: d["indicators"][0].update(primary_key=["countryiso3code", "staging_merge_id"])),
    "pk_unknown_column": _mut(lambda d: d["indicators"][0].update(primary_key=["countryiso3code", "nope"])),
    "pk_without_countryiso3code": _mut(lambda d: d["indicators"][0].update(primary_key=["date"])),
    "pk_empty": _mut(lambda d: d["indicators"][0].update(primary_key=[])),
    "negative_max_retries": _mut(lambda d: d["retry"].update(max_retries=-1)),
    "negative_sleep": _mut(lambda d: d["retry"].update(sleep_seconds=-1)),
    "retry_on_5xx_not_bool": _mut(lambda d: d["retry"].update(retry_on_5xx="yes")),
    "retry_on_connection_errors_not_bool": _mut(lambda d: d["retry"].update(retry_on_connection_errors="yes")),
    "analysis_table_unknown_code": _mut(lambda d: d["analysis"]["tables"].update(life_expectancy="NOT.CONFIGURED")),
    "indicator_code_with_slash": _mut(lambda d: d["indicators"][0].update(code="SP/POP")),
    "indicator_code_with_pipe": _mut(lambda d: d["indicators"][0].update(code="SP|POP")),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_config_raises_config_error(tmp_path, name):
    d = INVALID[name](base_cfg_dict())
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, d))


def test_valid_dates_boundary_start_equals_end(tmp_path):
    d = base_cfg_dict()
    d["date_range"].update(start=2020, end=2020)
    assert _load(tmp_path, d)["date_range"]["start"] == 2020


def test_custom_primary_key_is_preserved(tmp_path):
    d = base_cfg_dict()
    d["indicators"][0]["primary_key"] = ["countryiso3code", "date", "unit"]
    cfg = _load(tmp_path, d)
    assert get_indicator(cfg, LE)["primary_key"] == ["countryiso3code", "date", "unit"]
    assert get_indicator(cfg, GDP)["primary_key"] == ["countryiso3code", "date"]
