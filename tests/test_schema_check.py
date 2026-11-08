"""schema_check (plan section 7)."""
import copy
import logging

import pytest

from helpers import GDP, LE, make_page, make_pull
from src.schema_check import SchemaCheckError, schema_check


def good(page=1, pages=1, per_page=3, total=3, years=(2024, 2023, 2022), country="BRA", ind=LE):
    return make_page(country, ind, list(years), page, pages, per_page, total)


def check(cfg, resp, page=1, country="BRA", ind=LE):
    return schema_check(resp, cfg, country, ind, page)


def errors(cfg, resp, **kw):
    with pytest.raises(SchemaCheckError) as ei:
        check(cfg, resp, **kw)
    assert ei.value.errors and all(isinstance(e, str) for e in ei.value.errors)
    return ei.value


def test_valid_page_passes(cfg):
    assert check(cfg, good()) is None


def test_valid_page_from_pull_pages(cfg):
    for n, pg in enumerate(make_pull("CAN", GDP), 1):
        assert check(cfg, pg, page=n, country="CAN", ind=GDP) is None


def test_error_is_a_pipeline_error():
    assert "PipelineError" in [c.__name__ for c in SchemaCheckError.__mro__]


def test_message_names_country_indicator_and_every_error(cfg):
    resp = good()
    del resp[0]["sourceid"]
    resp[1][0]["indicator"]["id"] = "WRONG"
    e = errors(cfg, resp)
    text = str(e)
    assert "BRA" in text and LE in text
    for err in e.errors:
        assert err in text


# ----------------------------------------------------------------------------- envelope
@pytest.mark.parametrize("resp", [None, "text", 5, {"page": 1}, ({}, []), []])
def test_envelope_must_be_list_of_two(cfg, resp):
    errors(cfg, resp)


def test_three_element_envelope_fails(cfg):
    errors(cfg, good() + [[]])


def test_api_error_body_is_reported_with_its_message(cfg):
    body = [{"message": [{"id": "120", "key": "Invalid value",
                          "value": "The provided parameter value is not valid"}]}]
    e = errors(cfg, body)
    assert "Invalid value" in str(e)


# ----------------------------------------------------------------------------- element 0 (metadata)
@pytest.mark.parametrize("key", ["page", "pages", "per_page", "total", "sourceid", "lastupdated"])
def test_missing_metadata_key_fails(cfg, key):
    resp = good()
    del resp[0][key]
    e = errors(cfg, resp)
    assert any(key in err for err in e.errors)


def test_metadata_not_a_dict_fails(cfg):
    resp = good()
    resp[0] = ["page", 1]
    errors(cfg, resp)


@pytest.mark.parametrize("key, bad", [
    ("page", "1"), ("page", 1.0), ("page", True), ("page", None),
    ("pages", "1"), ("pages", True), ("pages", 1.5),
    ("total", "3"), ("total", 3.0), ("total", True),
    ("per_page", 3.5), ("per_page", None), ("per_page", "abc"), ("per_page", ""), ("per_page", "3.5"),
    ("sourceid", 2), ("sourceid", None),
    ("lastupdated", 20261008), ("lastupdated", None),
])
def test_wrong_metadata_type_fails(cfg, key, bad):
    resp = good()
    resp[0][key] = bad
    e = errors(cfg, resp)
    assert any(key in err for err in e.errors)


@pytest.mark.parametrize("per_page", [3, "3", "50"])
def test_per_page_int_or_digit_string_passes(cfg, per_page):
    resp = good()
    resp[0]["per_page"] = per_page
    assert check(cfg, resp) is None


def test_metadata_page_must_equal_expected_page(cfg):
    resp = good(page=2, pages=3)
    assert check(cfg, resp, page=2) is None
    errors(cfg, resp, page=1)
    errors(cfg, resp, page=3)


@pytest.mark.parametrize("key, bad", [("pages", 0), ("pages", -1), ("total", 0), ("total", -2)])
def test_pages_and_total_must_be_at_least_one(cfg, key, bad):
    resp = good()
    resp[0][key] = bad
    errors(cfg, resp)


def test_extra_metadata_key_tolerated_with_warning(cfg, caplog):
    resp = good()
    resp[0]["new_field"] = "x"
    with caplog.at_level(logging.WARNING):
        assert check(cfg, resp) is None
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ----------------------------------------------------------------------------- element 1 (records)
@pytest.mark.parametrize("records", [None, [], "rows", {"a": 1}, 7])
def test_records_must_be_non_empty_list(cfg, records):
    resp = good()
    resp[1] = records
    errors(cfg, resp)


def test_record_must_be_dict(cfg):
    resp = good()
    resp[1][1] = "not a dict"
    errors(cfg, resp)


@pytest.mark.parametrize("key", ["indicator", "country", "countryiso3code", "date", "value", "unit",
                                 "obs_status", "decimal"])
def test_missing_record_key_fails(cfg, key):
    resp = good()
    del resp[1][0][key]
    e = errors(cfg, resp)
    assert any(key in err for err in e.errors)


def test_missing_key_in_later_record_is_reported_with_its_index(cfg):
    resp = good()
    del resp[1][2]["unit"]
    e = errors(cfg, resp)
    assert any("unit" in err and "2" in err for err in e.errors)


@pytest.mark.parametrize("path, bad", [
    (("indicator",), "SP.DYN.LE00.IN"), (("indicator",), None),
    (("country",), "BR"), (("country",), None),
])
def test_nested_objects_must_be_dicts(cfg, path, bad):
    resp = good()
    resp[1][0][path[0]] = bad
    errors(cfg, resp)


@pytest.mark.parametrize("obj, key", [("indicator", "id"), ("indicator", "value"),
                                      ("country", "id"), ("country", "value")])
def test_nested_object_missing_key_fails(cfg, obj, key):
    resp = good()
    del resp[1][0][obj][key]
    errors(cfg, resp)


@pytest.mark.parametrize("obj, key", [("indicator", "id"), ("indicator", "value"),
                                      ("country", "id"), ("country", "value")])
def test_nested_object_non_string_fails(cfg, obj, key):
    resp = good()
    resp[1][0][obj][key] = 5
    errors(cfg, resp)


@pytest.mark.parametrize("value", [123456789, 70.5, 0, 0.0, -1.5])
def test_numeric_values_allowed(cfg, value):
    resp = good()
    resp[1][0]["value"] = value
    assert check(cfg, resp) is None


def test_null_value_allowed(cfg):
    resp = good()
    resp[1][1]["value"] = None
    assert check(cfg, resp) is None


@pytest.mark.parametrize("value", ["123", "", True, False, [1], {"v": 1}])
def test_bad_value_types_fail(cfg, value):
    resp = good()
    resp[1][0]["value"] = value
    errors(cfg, resp)


@pytest.mark.parametrize("key, bad", [
    ("countryiso3code", 5), ("countryiso3code", None),
    ("date", 2022), ("date", None),
    ("unit", 5), ("unit", None), ("obs_status", 1), ("obs_status", None),
    ("decimal", "0"), ("decimal", 0.0), ("decimal", None),
])
def test_bad_record_field_types_fail(cfg, key, bad):
    resp = good()
    resp[1][0][key] = bad
    errors(cfg, resp)


@pytest.mark.parametrize("code", ["CAN", "bra", "BR", "BRAZ", "", "BR1"])
def test_countryiso3code_must_equal_requested_country(cfg, code):
    resp = good()
    resp[1][1]["countryiso3code"] = code
    e = errors(cfg, resp)
    assert any("countryiso3code" in err for err in e.errors)


def test_indicator_id_must_equal_requested_indicator(cfg):
    resp = good()
    resp[1][0]["indicator"]["id"] = GDP
    e = errors(cfg, resp)
    assert any("indicator" in err for err in e.errors)


@pytest.mark.parametrize("date", ["18", "201", "20180", "2017", "2025", "1999", "abcd", "2018-01",
                                  "2020M01", "2020Q1", " 2020", ""])
def test_date_must_be_four_digits_inside_range(cfg, date):
    resp = good()
    resp[1][0]["date"] = date
    e = errors(cfg, resp)
    assert any("date" in err for err in e.errors)


@pytest.mark.parametrize("date", ["2018", "2024", "2021"])
def test_date_range_boundaries_are_inclusive(cfg, date):
    resp = good()
    resp[1][0]["date"] = date
    assert check(cfg, resp) is None


def test_date_range_comes_from_config(cfg_dict, load_cfg):
    cfg_dict["date_range"] = {"start": 2019, "end": 2020}
    cfg = load_cfg(cfg_dict)
    resp = good()
    resp[1][0]["date"] = "2019"
    resp[1][1]["date"] = "2020"
    resp[1][2]["date"] = "2020"
    assert check(cfg, resp) is None
    resp[1][2]["date"] = "2018"
    errors(cfg, resp)
    resp[1][2]["date"] = "2021"
    errors(cfg, resp)


def test_extra_record_key_tolerated_with_warning(cfg, caplog):
    resp = good()
    resp[1][0]["new_field"] = "x"
    with caplog.at_level(logging.WARNING):
        assert check(cfg, resp) is None
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_checks_use_arguments_not_hardcoded_codes(cfg):
    resp = good(country="CAN", ind=GDP)
    assert check(cfg, resp, country="CAN", ind=GDP) is None
    errors(cfg, resp, country="BRA", ind=GDP)
    errors(cfg, resp, country="CAN", ind=LE)


# ----------------------------------------------------------------------------- all violations reported
def test_all_violations_in_both_elements_are_reported_at_once(cfg):
    resp = good()
    del resp[0]["total"]
    resp[0]["pages"] = "1"
    resp[1][0]["countryiso3code"] = "CAN"
    resp[1][1]["date"] = "1999"
    del resp[1][2]["decimal"]
    e = errors(cfg, resp)
    joined = " ".join(e.errors)
    for needle in ("total", "pages", "countryiso3code", "date", "decimal"):
        assert needle in joined
    assert len(e.errors) >= 5


def test_schema_check_does_not_mutate_the_response(cfg):
    resp = good()
    before = copy.deepcopy(resp)
    check(cfg, resp)
    assert resp == before
