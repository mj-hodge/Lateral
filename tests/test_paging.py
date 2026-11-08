"""Paging in fetch_pull (plan sections 5 and 6)."""
import copy
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from helpers import FakeHttpGet, FakeResponse, GDP, LE, base_cfg_dict, drop_last_record, make_pull, rid_at
from src import api
from src.api import RateLimitExhausted
from src.schema_check import SchemaCheckError

RID = rid_at(0)


def pull(cfg, fake, sleep, country="BRA", ind=LE):
    return api.fetch_pull(cfg, country, ind, RID, http_get=fake, sleep=sleep)


def q(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_three_pages_requested_in_order_and_returned_raw(cfg, fake_sleep):
    expected = make_pull("BRA", LE)
    fake = FakeHttpGet(pulls={("BRA", LE): expected}, auto=False)
    pages = pull(cfg, fake, fake_sleep)
    assert fake.keys == [("BRA", LE, 1), ("BRA", LE, 2), ("BRA", LE, 3)]
    assert [q(u)["page"] for u in fake.calls] == ["1", "2", "3"]
    assert all(q(u)["per_page"] == "3" for u in fake.calls)
    assert pages == expected
    assert [len(p[1]) for p in pages] == [3, 3, 1]
    assert fake_sleep.calls == []


def test_single_page_when_pages_is_one(cfg, fake_sleep):
    expected = make_pull("CAN", GDP, per_page=50)
    assert len(expected) == 1
    fake = FakeHttpGet(pulls={("CAN", GDP): expected}, auto=False)
    pages = pull(cfg, fake, fake_sleep, "CAN", GDP)
    assert fake.keys == [("CAN", GDP, 1)]
    assert pages == expected


def test_pages_count_comes_from_page_one_metadata(cfg, fake_sleep):
    expected = make_pull("BRA", LE, per_page=2)  # 7 records, 4 pages
    assert len(expected) == 4
    fake = FakeHttpGet(pulls={("BRA", LE): expected}, auto=False)
    pages = pull(cfg, fake, fake_sleep)
    assert [k[2] for k in fake.keys] == [1, 2, 3, 4]
    assert pages == expected


def test_no_page_beyond_pages_requested(cfg, fake_sleep):
    fake = FakeHttpGet()
    pull(cfg, fake, fake_sleep)
    assert max(k[2] for k in fake.keys) == 3


def test_does_not_assert_record_count_equals_total(cfg, fake_sleep):
    """Record count vs total is row_count's job, not fetch_pull's."""
    short = drop_last_record(make_pull("BRA", LE))
    fake = FakeHttpGet(pulls={("BRA", LE): short}, auto=False)
    pages = pull(cfg, fake, fake_sleep)
    assert pages == short
    assert sum(len(p[1]) for p in pages) == 6 and pages[0][0]["total"] == 7


def test_null_value_record_is_kept(cfg, fake_sleep):
    p = make_pull("BRA", LE, values={2020: None})
    fake = FakeHttpGet(pulls={("BRA", LE): p}, auto=False)
    pages = pull(cfg, fake, fake_sleep)
    assert [r["value"] for pg in pages for r in pg[1] if r["date"] == "2020"] == [None]


def test_retries_per_page_do_not_share_budget(cfg, fake_sleep):
    p = make_pull("BRA", LE)
    r429 = FakeResponse(429, None, text="slow down")
    responses = {("BRA", LE, 1): (r429, r429, r429, p[0]),
                 ("BRA", LE, 2): (r429, r429, r429, p[1]),
                 ("BRA", LE, 3): (p[2],)}
    fake = FakeHttpGet(responses=responses, auto=False)
    pages = pull(cfg, fake, fake_sleep)
    assert pages == p
    assert fake_sleep.calls == [60] * 6
    assert len(fake.calls) == 9   # 4 + 4 + 1 attempts


def test_exhausted_retries_on_a_later_page_fail_the_pull(cfg, fake_sleep):
    p = make_pull("BRA", LE)
    r429 = FakeResponse(429, None, text="x")
    fake = FakeHttpGet(responses={("BRA", LE, 1): (p[0],), ("BRA", LE, 2): (r429,) * 4}, auto=False)
    with pytest.raises(RateLimitExhausted):
        pull(cfg, fake, fake_sleep)
    assert fake_sleep.calls == [60, 60, 60]


def test_uses_given_request_id_without_calling_a_clock(cfg, fake_sleep, monkeypatch):
    """fetch_pull never generates a request_id; its signature has no `now`."""
    import inspect
    assert "now" not in inspect.signature(api.fetch_pull).parameters


# ----------------------------------------------------------------------------- cross-page consistency
def _tampered(mutator):
    p = copy.deepcopy(make_pull("BRA", LE))
    mutator(p)
    return p


@pytest.mark.parametrize("name, mutator", [
    ("total_changes_on_page_2", lambda p: p[1][0].__setitem__("total", 8)),
    ("total_changes_on_page_3", lambda p: p[2][0].__setitem__("total", 6)),
    ("pages_changes_on_page_2", lambda p: p[1][0].__setitem__("pages", 4)),
    ("page_2_reports_wrong_page", lambda p: p[1][0].__setitem__("page", 3)),
    ("page_2_reports_page_1", lambda p: p[1][0].__setitem__("page", 1)),
    ("page_1_reports_page_2", lambda p: p[0][0].__setitem__("page", 2)),
])
def test_metadata_mismatch_raises(cfg, fake_sleep, name, mutator):
    fake = FakeHttpGet(pulls={("BRA", LE): _tampered(mutator)}, auto=False)
    with pytest.raises(SchemaCheckError):
        pull(cfg, fake, fake_sleep)


def test_invalid_page_in_the_middle_raises_schema_check_error(cfg, fake_sleep):
    p = _tampered(lambda p: p[1][1][0]["indicator"].__setitem__("id", "WRONG.CODE"))
    fake = FakeHttpGet(pulls={("BRA", LE): p}, auto=False)
    with pytest.raises(SchemaCheckError):
        pull(cfg, fake, fake_sleep)


@pytest.mark.parametrize("mutator", [
    lambda p: [m.__setitem__("pages", 0) for m in (pg[0] for pg in p)],
    lambda p: [m.__setitem__("total", 0) for m in (pg[0] for pg in p)],
])
def test_empty_pull_fails(cfg, fake_sleep, mutator):
    p = _tampered(mutator)
    fake = FakeHttpGet(pulls={("BRA", LE): p}, auto=False)
    with pytest.raises(SchemaCheckError):
        pull(cfg, fake, fake_sleep)


def test_null_records_fail(cfg, fake_sleep):
    p = make_pull("BRA", LE, per_page=50)
    p[0][1] = None
    fake = FakeHttpGet(pulls={("BRA", LE): p}, auto=False)
    with pytest.raises(SchemaCheckError):
        pull(cfg, fake, fake_sleep)


def test_api_error_body_with_http_200_fails(cfg, fake_sleep):
    body = [{"message": [{"id": "120", "key": "Invalid value",
                          "value": "The provided parameter value is not valid"}]}]
    fake = FakeHttpGet(responses={("BRA", LE, 1): (FakeResponse(200, body),)}, auto=False)
    with pytest.raises(SchemaCheckError) as ei:
        pull(cfg, fake, fake_sleep)
    assert "Invalid value" in str(ei.value)


def test_duplicate_primary_key_across_pages_with_matching_total_raises(cfg, fake_sleep):
    """Unordered API: page 2 repeats a year from page 1 and drops another; count still equals total."""
    p = make_pull("BRA", LE)
    p[1][1][0] = copy.deepcopy(p[0][1][0])  # page 2 now repeats page 1's first record
    assert sum(len(pg[1]) for pg in p) == p[0][0]["total"]
    fake = FakeHttpGet(pulls={("BRA", LE): p}, auto=False)
    with pytest.raises(SchemaCheckError):
        pull(cfg, fake, fake_sleep)


def test_duplicate_detection_uses_configured_primary_key(load_cfg, fake_sleep):
    """With PK [countryiso3code, date, unit] two records with equal date but different unit are distinct."""
    d = base_cfg_dict()
    d["indicators"][0]["primary_key"] = ["countryiso3code", "date", "unit"]
    cfg = load_cfg(d)
    p = make_pull("BRA", LE)
    p[1][1][0] = copy.deepcopy(p[0][1][0])
    p[1][1][0]["unit"] = "other"
    fake = FakeHttpGet(pulls={("BRA", LE): p}, auto=False)
    assert pull(cfg, fake, fake_sleep) == p


def test_corrected_pull_succeeds_after_a_failed_one(cfg, fake_sleep):
    bad = make_pull("BRA", LE)
    bad[1][1][0] = copy.deepcopy(bad[0][1][0])
    fake = FakeHttpGet(pulls={("BRA", LE): bad}, auto=False)
    with pytest.raises(SchemaCheckError):
        pull(cfg, fake, fake_sleep)
    good = make_pull("BRA", LE)
    fake2 = FakeHttpGet(pulls={("BRA", LE): good}, auto=False)
    assert pull(cfg, fake2, fake_sleep) == good
