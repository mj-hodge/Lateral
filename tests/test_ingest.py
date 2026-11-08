"""Ingestion: file/log naming and format, request_id sharing, atomic landing, recovery rules
(plan sections 5, 6, 8, 14 and the Landing recovery row of section 15)."""
import json
import logging
import re

import pytest
import requests

from helpers import (find_exc, GDP, LE, RID_OLD1, RID_OLD2, RID_OLD3, FakeHttpGet, FakeResponse, drop_last_record,
                     files_in, land_pull, landing_dir, log_path, make_pull, marker_path, page_name,
                     rejected_dir, rid_at, write_log, write_report)
from src import ingest
from src.api import RateLimitExhausted, RetryExhausted
from src.schema_check import SchemaCheckError

def collision_error():
    return find_exc("FileCollisionError")


def staging_error():
    import src.staging as staging
    return staging.StagingError


# ----------------------------------------------------------------------------- naming and format helpers
def test_landing_filename():
    assert ingest.landing_filename("BRA", "SP.POP.TOTL", "20261108T153045123456", 1) == \
        "BRA_SP.POP.TOTL_20261108T153045123456_p1.json"
    assert ingest.landing_filename("CAN", LE, rid_at(0), 12) == f"CAN_{LE}_{rid_at(0)}_p12.json"


def test_format_log_line():
    line = ingest.format_log_line("20261108T153045123456", 7, 3, "BRA", "SP.POP.TOTL")
    assert line == "20261108T153045123456|7|3|BRA|SP.POP.TOTL"


def test_write_request_log(cfg, tmp_path):
    p = ingest.write_request_log(cfg, rid_at(0), 7, 3, "BRA", LE)
    assert p == tmp_path / "log" / LE / f"{rid_at(0)}.log"
    assert p.read_bytes() == f"{rid_at(0)}|7|3|BRA|{LE}\n".encode()


def test_write_request_log_one_file_per_request_id(cfg, tmp_path):
    ingest.write_request_log(cfg, rid_at(0), 7, 3, "BRA", LE)
    ingest.write_request_log(cfg, rid_at(1), 7, 3, "CAN", LE)
    assert files_in(tmp_path / "log" / LE) == sorted([f"{rid_at(0)}.log", f"{rid_at(1)}.log"])


# ----------------------------------------------------------------------------- write_landing_pages
def test_write_landing_pages_one_file_per_page_with_raw_content(cfg, tmp_path):
    pull = make_pull("BRA", LE)
    paths = ingest.write_landing_pages(cfg, "BRA", LE, rid_at(0), pull)
    assert [p.name for p in paths] == [page_name("BRA", LE, rid_at(0), n) for n in (1, 2, 3)]
    assert all(p.parent == landing_dir(tmp_path, LE) for p in paths)
    for p, page in zip(paths, pull):
        assert json.loads(p.read_text()) == page
    assert files_in(landing_dir(tmp_path, LE)) == sorted(p.name for p in paths)  # no .part files left


def test_write_landing_pages_collision_raises_and_writes_nothing(cfg, tmp_path):
    pull = make_pull("BRA", LE)
    existing = landing_dir(tmp_path, LE) / page_name("BRA", LE, rid_at(0), 2)
    existing.parent.mkdir(parents=True)
    existing.write_text("ORIGINAL")
    with pytest.raises(collision_error()):
        ingest.write_landing_pages(cfg, "BRA", LE, rid_at(0), pull)
    assert files_in(landing_dir(tmp_path, LE)) == [existing.name]
    assert existing.read_text() == "ORIGINAL"


# ----------------------------------------------------------------------------- ingest_pair
def test_ingest_pair_writes_pages_and_log_sharing_one_request_id(cfg, tmp_path, fake_sleep, fake_now):
    fake = FakeHttpGet()
    res = ingest.ingest_pair(cfg, "BRA", LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    rid = res["request_id"]
    assert rid == rid_at(0)
    assert re.fullmatch(r"\d{8}T\d{12}", rid) and len(rid) == 21
    assert res["total"] == 7 and res["pages"] == 3
    assert fake_now.count == 1  # one request_id per pull, not per page
    assert len(fake.calls) == 3

    expected_names = [f"BRA_{LE}_{rid}_p{n}.json" for n in (1, 2, 3)]
    assert files_in(landing_dir(tmp_path, LE)) == expected_names
    assert [p.name for p in res["landing_paths"]] == expected_names
    pull = make_pull("BRA", LE)
    for n, name in enumerate(expected_names):
        assert json.loads((landing_dir(tmp_path, LE) / name).read_text()) == pull[n]

    lp = log_path(tmp_path, LE, rid)
    assert res["log_path"] == lp
    assert files_in(tmp_path / "log" / LE) == [f"{rid}.log"]
    assert lp.read_bytes() == f"{rid}|7|3|BRA|{LE}\n".encode()
    assert fake_sleep.calls == []


def test_ingest_pair_log_values_come_from_first_page_metadata(cfg, tmp_path, fake_sleep, fake_now):
    pull = make_pull("BRA", LE, per_page=50)
    fake = FakeHttpGet(pulls={("BRA", LE): pull}, auto=False)
    cfg["api"]["per_page"] = 50
    res = ingest.ingest_pair(cfg, "BRA", LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert log_path(tmp_path, LE, res["request_id"]).read_text() == f"{res['request_id']}|7|1|BRA|{LE}\n"
    assert len(res["landing_paths"]) == 1


def test_two_pulls_get_different_request_ids(cfg, fake_sleep, fake_now):
    fake = FakeHttpGet()
    a = ingest.ingest_pair(cfg, "BRA", LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    b = ingest.ingest_pair(cfg, "CAN", LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert a["request_id"] != b["request_id"]
    assert (a["request_id"], b["request_id"]) == (rid_at(0), rid_at(1))


def test_ingest_pair_retries_use_injected_sleep(cfg, fake_sleep, fake_now):
    p = make_pull("BRA", LE)
    r429 = FakeResponse(429, None, text="x")
    fake = FakeHttpGet(responses={("BRA", LE, 2): (r429, r429, p[1])}, pulls={("BRA", LE): p})
    res = ingest.ingest_pair(cfg, "BRA", LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert fake_sleep.calls == [60, 60]
    assert res["pages"] == 3


@pytest.mark.parametrize("responses, exc", [
    ({("BRA", LE, 2): (FakeResponse(429, None, text="x"),) * 4}, RateLimitExhausted),
    ({("BRA", LE, 1): (FakeResponse(503, None, text="x"),) * 4}, RetryExhausted),
    ({("BRA", LE, 3): (requests.ConnectionError("down"),) * 4}, RetryExhausted),
])
def test_failed_pull_leaves_no_files(cfg, tmp_path, fake_sleep, fake_now, responses, exc):
    fake = FakeHttpGet(responses=responses)
    with pytest.raises(exc):
        ingest.ingest_pair(cfg, "BRA", LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert files_in(landing_dir(tmp_path, LE)) == []
    assert files_in(tmp_path / "log" / LE) == []


def test_schema_failure_leaves_no_landing_log_or_part_files(cfg, tmp_path, fake_sleep, fake_now):
    pull = make_pull("BRA", LE)
    pull[2][1][0]["indicator"]["id"] = "WRONG.CODE"  # only the last page is bad
    fake = FakeHttpGet(pulls={("BRA", LE): pull})
    with pytest.raises(SchemaCheckError):
        ingest.ingest_pair(cfg, "BRA", LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert files_in(landing_dir(tmp_path, LE)) == []
    assert files_in(tmp_path / "log" / LE) == []


def test_duplicate_pk_pull_leaves_nothing_and_next_run_succeeds(cfg, tmp_path, fake_sleep, fake_now):
    bad = make_pull("BRA", LE)
    bad[1][1][0] = json.loads(json.dumps(bad[0][1][0]))
    with pytest.raises(SchemaCheckError):
        ingest.ingest_pair(cfg, "BRA", LE, http_get=FakeHttpGet(pulls={("BRA", LE): bad}),
                           sleep=fake_sleep, now=fake_now)
    assert files_in(landing_dir(tmp_path, LE)) == [] and files_in(tmp_path / "log" / LE) == []
    res = ingest.ingest_pair(cfg, "BRA", LE, http_get=FakeHttpGet(), sleep=fake_sleep, now=fake_now)
    assert len(files_in(landing_dir(tmp_path, LE))) == 3 and res["pages"] == 3


def test_write_failure_leaves_no_final_and_no_part_files(cfg, tmp_path, fake_sleep, fake_now):
    """An extra metadata key passes the schema check (tolerated) but cannot be serialized to JSON,
    so writing page 3 fails after pages 1-2 were already prepared."""
    pull = make_pull("BRA", LE)
    pull[2][0]["extra"] = {1, 2, 3}
    fake = FakeHttpGet(pulls={("BRA", LE): pull})
    with pytest.raises(Exception):
        ingest.ingest_pair(cfg, "BRA", LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert files_in(landing_dir(tmp_path, LE)) == []  # neither *.json nor *.part


def test_ingest_pair_refuses_to_overwrite_existing_log(cfg, tmp_path, fake_sleep, fake_now):
    existing = write_log(tmp_path, rid_at(0), 99, 99, "XXX", LE)
    with pytest.raises(collision_error()):
        ingest.ingest_pair(cfg, "BRA", LE, http_get=FakeHttpGet(), sleep=fake_sleep, now=fake_now)
    assert existing.read_text() == f"{rid_at(0)}|99|99|XXX|{LE}\n"
    assert files_in(landing_dir(tmp_path, LE)) == []


def test_ingest_pair_refuses_to_overwrite_existing_page_file(cfg, tmp_path, fake_sleep, fake_now):
    d = landing_dir(tmp_path, LE)
    d.mkdir(parents=True)
    existing = d / page_name("BRA", LE, rid_at(0), 3)
    existing.write_text("ORIGINAL")
    with pytest.raises(collision_error()):
        ingest.ingest_pair(cfg, "BRA", LE, http_get=FakeHttpGet(), sleep=fake_sleep, now=fake_now)
    assert files_in(d) == [existing.name] and existing.read_text() == "ORIGINAL"


# ----------------------------------------------------------------------------- find_existing_landing / check_page_set
def test_find_existing_landing_groups_by_request_id_and_sorts_pages_numerically(cfg, tmp_path):
    d = landing_dir(tmp_path, LE)
    d.mkdir(parents=True)
    for rid in (RID_OLD2, RID_OLD1):
        for n in (10, 2, 1):
            (d / page_name("BRA", LE, rid, n)).write_text("[]")
    (d / page_name("CAN", LE, RID_OLD1, 1)).write_text("[]")
    (d / (page_name("BRA", LE, RID_OLD3, 1) + ".part")).write_text("[]")
    found = ingest.find_existing_landing(cfg, LE, "BRA")
    assert list(found) == [RID_OLD1, RID_OLD2]  # ascending request_id, no CAN, no .part
    assert [p.name for p in found[RID_OLD1]] == [page_name("BRA", LE, RID_OLD1, n) for n in (1, 2, 10)]
    assert all(p.parent == d for p in found[RID_OLD2])


def test_find_existing_landing_empty(cfg):
    assert ingest.find_existing_landing(cfg, LE, "BRA") == {}


def test_find_existing_landing_does_not_mix_indicators(cfg, tmp_path):
    land_pull(tmp_path, "BRA", GDP, RID_OLD1)
    assert ingest.find_existing_landing(cfg, LE, "BRA") == {}
    assert list(ingest.find_existing_landing(cfg, GDP, "BRA")) == [RID_OLD1]


def test_check_page_set_complete_passes():
    pages = {n + 1: p for n, p in enumerate(make_pull("BRA", LE))}
    assert ingest.check_page_set(pages) is None


def test_check_page_set_single_page_passes():
    assert ingest.check_page_set({1: make_pull("BRA", LE, per_page=50)[0]}) is None


def _pages(**kw):
    return {n + 1: p for n, p in enumerate(make_pull("BRA", LE))}


@pytest.mark.parametrize("name, mutate", [
    ("missing_page_2", lambda d: d.pop(2)),
    ("missing_page_1", lambda d: d.pop(1)),
    ("missing_last_page", lambda d: d.pop(3)),
    ("extra_page_4", lambda d: d.__setitem__(4, d[3])),
    ("pages_disagree", lambda d: d[2][0].__setitem__("pages", 4)),
    ("total_disagree", lambda d: d[3][0].__setitem__("total", 8)),
    ("name_page_differs_from_metadata", lambda d: d.update({2: d[3], 3: d[2]})),
])
def test_check_page_set_rejects_bad_sets(name, mutate):
    pages = _pages()
    mutate(pages)
    with pytest.raises(staging_error()):
        ingest.check_page_set(pages)


# ----------------------------------------------------------------------------- move_request_files
@pytest.mark.parametrize("dest", ["archived", "rejected"])
def test_move_request_files_moves_whole_set(cfg, tmp_path, dest):
    paths = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    other = land_pull(tmp_path, "CAN", LE, RID_OLD2)
    contents = [p.read_text() for p in paths]
    moved = ingest.move_request_files(cfg, LE, RID_OLD1, dest)
    target = tmp_path / "storage" / dest / LE
    assert sorted(p.name for p in moved) == sorted(p.name for p in paths)
    assert all(p.parent == target and p.exists() for p in moved)
    assert [ (target / p.name).read_text() for p in paths] == contents
    assert files_in(landing_dir(tmp_path, LE)) == sorted(p.name for p in other)  # other group untouched


def test_move_request_files_collision_moves_nothing(cfg, tmp_path):
    paths = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    target = tmp_path / "storage" / "archived" / LE
    target.mkdir(parents=True)
    clash = target / paths[1].name
    clash.write_text("PRE-EXISTING")
    with pytest.raises(collision_error()):
        ingest.move_request_files(cfg, LE, RID_OLD1, "archived")
    assert files_in(landing_dir(tmp_path, LE)) == sorted(p.name for p in paths)
    assert files_in(target) == [clash.name] and clash.read_text() == "PRE-EXISTING"


# ----------------------------------------------------------------------------- marker + failed_request_ids
def test_mark_staging_failed_writes_sorted_union_without_duplicates(cfg, tmp_path):
    ingest.mark_staging_failed(cfg, LE, [RID_OLD2])
    ingest.mark_staging_failed(cfg, LE, [RID_OLD3, RID_OLD1, RID_OLD2])
    text = marker_path(tmp_path, LE).read_text()
    assert text.splitlines() == [RID_OLD1, RID_OLD2, RID_OLD3]
    assert text.endswith("\n") or text.count("\n") == 2
    assert files_in(marker_path(tmp_path, LE).parent) == [marker_path(tmp_path, LE).name]


def test_clear_staging_marks_removes_ids_and_deletes_empty_file(cfg, tmp_path):
    ingest.mark_staging_failed(cfg, LE, [RID_OLD1, RID_OLD2])
    ingest.clear_staging_marks(cfg, LE, [RID_OLD1, "20990101T000000000000"])  # unknown id is harmless
    assert marker_path(tmp_path, LE).read_text().splitlines() == [RID_OLD2]
    ingest.clear_staging_marks(cfg, LE, [RID_OLD2])
    assert not marker_path(tmp_path, LE).exists()


def test_clear_on_missing_marker_is_a_noop(cfg, tmp_path):
    ingest.clear_staging_marks(cfg, LE, [RID_OLD1])
    assert not marker_path(tmp_path, LE).exists()


def test_failed_request_ids_empty_when_nothing_recorded(cfg):
    assert ingest.failed_request_ids(cfg, LE) == set()


def test_failed_request_ids_from_marker(cfg):
    ingest.mark_staging_failed(cfg, LE, [RID_OLD1, RID_OLD3])
    assert ingest.failed_request_ids(cfg, LE) == {RID_OLD1, RID_OLD3}


def test_failed_request_ids_from_latest_report_only_fail_rows(cfg, tmp_path):
    write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 7, "PASS"), (RID_OLD2, "CAN", 7, 6, "FAIL")])
    assert ingest.failed_request_ids(cfg, LE) == {RID_OLD2}


def test_failed_request_ids_uses_greatest_filename_not_mtime(cfg, tmp_path):
    newer = write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 7, "PASS")],
                         smid="aaaaaaaa-0000-4000-8000-000000000002", ts="20261108T170000000000")
    older = write_report(tmp_path, LE, [(RID_OLD2, "CAN", 7, 6, "FAIL")],
                         smid="aaaaaaaa-0000-4000-8000-000000000001", ts="20261108T150000000000")
    # written last -> newest mtime, but earliest timestamp in its name
    assert ingest.failed_request_ids(cfg, LE) == set()
    newer.unlink()
    older.unlink()
    write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 7, "PASS")], ts="20261108T150000000000",
                 smid="bbbbbbbb-0000-4000-8000-000000000001")
    write_report(tmp_path, LE, [(RID_OLD3, "CAN", 7, 6, "FAIL")], ts="20261108T170000000000",
                 smid="bbbbbbbb-0000-4000-8000-000000000002")
    assert ingest.failed_request_ids(cfg, LE) == {RID_OLD3}


def test_failed_request_ids_is_union_of_report_and_marker(cfg, tmp_path):
    write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 6, "FAIL")])
    ingest.mark_staging_failed(cfg, LE, [RID_OLD2])
    assert ingest.failed_request_ids(cfg, LE) == {RID_OLD1, RID_OLD2}


# ----------------------------------------------------------------------------- ingest_indicator
def test_ingest_indicator_pulls_every_country_sequentially(cfg, tmp_path, fake_sleep, fake_now):
    fake = FakeHttpGet()
    results = ingest.ingest_indicator(cfg, LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert len(results) == 2
    assert fake.keys == [("BRA", LE, 1), ("BRA", LE, 2), ("BRA", LE, 3),
                         ("CAN", LE, 1), ("CAN", LE, 2), ("CAN", LE, 3)]
    assert fake_now.count == 2
    assert len(files_in(landing_dir(tmp_path, LE))) == 6
    assert len(files_in(tmp_path / "log" / LE)) == 2
    assert {r["request_id"] for r in results} == {rid_at(0), rid_at(1)}


def test_ingest_indicator_other_indicator_untouched(cfg, tmp_path, fake_sleep, fake_now):
    fake = FakeHttpGet()
    ingest.ingest_indicator(cfg, LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert {k[1] for k in fake.keys} == {LE}
    assert not (tmp_path / "storage" / "landing" / GDP).exists()


def test_resume_skips_pairs_whose_newest_group_is_complete(cfg, tmp_path, fake_sleep, fake_now):
    land_pull(tmp_path, "BRA", LE, RID_OLD1)
    before = files_in(landing_dir(tmp_path, LE))
    fake = FakeHttpGet()
    ingest.ingest_indicator(cfg, LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert {k[0] for k in fake.keys} == {"CAN"} and len(fake.keys) == 3
    assert set(before) <= set(files_in(landing_dir(tmp_path, LE)))
    assert len(files_in(landing_dir(tmp_path, LE))) == 6
    assert fake_now.count == 1


def test_second_call_makes_no_requests(cfg, tmp_path, fake_sleep, fake_now):
    ingest.ingest_indicator(cfg, LE, http_get=FakeHttpGet(), sleep=fake_sleep, now=fake_now)
    snapshot = {n: (landing_dir(tmp_path, LE) / n).read_text() for n in files_in(landing_dir(tmp_path, LE))}
    fake2 = FakeHttpGet(auto=False)
    ingest.ingest_indicator(cfg, LE, http_get=fake2, sleep=fake_sleep, now=fake_now)
    assert fake2.calls == []
    assert {n: (landing_dir(tmp_path, LE) / n).read_text() for n in files_in(landing_dir(tmp_path, LE))} == snapshot
    assert not rejected_dir(tmp_path, LE).exists() or files_in(rejected_dir(tmp_path, LE)) == []


def test_failure_on_second_country_keeps_first_countrys_files_for_resume(cfg, tmp_path, fake_sleep, fake_now):
    r429 = FakeResponse(429, None, text="x")
    fake = FakeHttpGet(responses={("CAN", LE, 1): (r429,) * 4})
    with pytest.raises(RateLimitExhausted):
        ingest.ingest_indicator(cfg, LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    names = files_in(landing_dir(tmp_path, LE))
    assert len(names) == 3 and all(n.startswith("BRA_") for n in names)
    # resume: only CAN is pulled
    fake2 = FakeHttpGet()
    ingest.ingest_indicator(cfg, LE, http_get=fake2, sleep=fake_sleep, now=fake_now)
    assert {k[0] for k in fake2.keys} == {"CAN"}


def test_marked_request_id_is_repulled_and_old_files_rejected(cfg, tmp_path, fake_sleep, fake_now):
    old = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    keep = land_pull(tmp_path, "CAN", LE, RID_OLD2)
    ingest.mark_staging_failed(cfg, LE, [RID_OLD1])
    fake = FakeHttpGet()
    ingest.ingest_indicator(cfg, LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert {k[0] for k in fake.keys} == {"BRA"} and len(fake.keys) == 3
    assert files_in(rejected_dir(tmp_path, LE)) == sorted(p.name for p in old)
    landing = files_in(landing_dir(tmp_path, LE))
    assert sorted(p.name for p in keep) == [n for n in landing if n.startswith("CAN_")]
    assert sorted(n for n in landing if n.startswith("BRA_")) == [
        page_name("BRA", LE, rid_at(0), n) for n in (1, 2, 3)]
    assert not marker_path(tmp_path, LE).exists()  # id cleared, empty marker file deleted


def test_failed_row_count_request_id_is_repulled_only_for_that_country(cfg, tmp_path, fake_sleep, fake_now):
    land_pull(tmp_path, "BRA", LE, RID_OLD1)
    can_old = land_pull(tmp_path, "CAN", LE, RID_OLD2)
    write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 7, "PASS"), (RID_OLD2, "CAN", 7, 6, "FAIL")])
    fake = FakeHttpGet()
    ingest.ingest_indicator(cfg, LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert {k[0] for k in fake.keys} == {"CAN"} and len(fake.keys) == 3
    assert files_in(rejected_dir(tmp_path, LE)) == sorted(p.name for p in can_old)
    assert len([n for n in files_in(landing_dir(tmp_path, LE)) if n.startswith("CAN_")]) == 3
    assert RID_OLD2 not in " ".join(files_in(landing_dir(tmp_path, LE)))


def test_refetch_rejects_every_old_group_and_pulls_all_pairs_anew(cfg, tmp_path, fake_sleep, fake_now):
    old_b = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    old_c = land_pull(tmp_path, "CAN", LE, RID_OLD2)
    fake = FakeHttpGet()
    ingest.ingest_indicator(cfg, LE, refetch=True, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert len(fake.keys) == 6
    assert files_in(rejected_dir(tmp_path, LE)) == sorted(p.name for p in old_b + old_c)
    landing = files_in(landing_dir(tmp_path, LE))
    assert len(landing) == 6 and not any(RID_OLD1 in n or RID_OLD2 in n for n in landing)


def test_two_groups_for_one_pair_keep_newest_and_reject_older_together(cfg, tmp_path, fake_sleep, fake_now):
    older = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    newer = land_pull(tmp_path, "BRA", LE, RID_OLD2)
    land_pull(tmp_path, "CAN", LE, RID_OLD3)
    fake = FakeHttpGet(auto=False)
    ingest.ingest_indicator(cfg, LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert fake.calls == []  # nothing marked, newest is complete -> no new pull
    assert files_in(rejected_dir(tmp_path, LE)) == sorted(p.name for p in older)
    names = files_in(landing_dir(tmp_path, LE))
    assert [n for n in names if n.startswith("BRA_")] == sorted(p.name for p in newer)


def test_incomplete_newest_group_is_rejected_and_pair_pulled_again(cfg, tmp_path, fake_sleep, fake_now):
    paths = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    paths[1].unlink()  # crash in the middle of the renames: only p1 and p3 remain
    land_pull(tmp_path, "CAN", LE, RID_OLD2)
    fake = FakeHttpGet()
    ingest.ingest_indicator(cfg, LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert {k[0] for k in fake.keys} == {"BRA"}
    assert files_in(rejected_dir(tmp_path, LE)) == sorted([paths[0].name, paths[2].name])
    bra = [n for n in files_in(landing_dir(tmp_path, LE)) if n.startswith("BRA_")]
    assert bra == [page_name("BRA", LE, rid_at(0), n) for n in (1, 2, 3)]


def test_group_of_country_removed_from_config_is_rejected_with_warning(cfg, tmp_path, fake_sleep, fake_now, caplog):
    gone = land_pull(tmp_path, "DEU", LE, RID_OLD1)
    fake = FakeHttpGet()
    with caplog.at_level(logging.WARNING):
        ingest.ingest_indicator(cfg, LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert files_in(rejected_dir(tmp_path, LE)) == sorted(p.name for p in gone)
    assert not any(n.startswith("DEU_") for n in files_in(landing_dir(tmp_path, LE)))
    assert {k[0] for k in fake.keys} == {"BRA", "CAN"}
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    assert not (tmp_path / "storage" / "archived" / LE).exists()


def test_stale_part_files_are_ignored_and_removed_by_next_ingest(cfg, tmp_path, fake_sleep, fake_now):
    d = landing_dir(tmp_path, LE)
    d.mkdir(parents=True)
    stale = d / (page_name("BRA", LE, RID_OLD1, 1) + ".part")
    stale.write_text("[]")
    fake = FakeHttpGet()
    ingest.ingest_indicator(cfg, LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert not stale.exists()
    assert not any(n.endswith(".part") for n in files_in(d))
    assert len(fake.keys) == 6  # a lone .part file is not a landing group


def test_after_ingest_staging_never_sees_more_than_one_group_per_pair(cfg, tmp_path, fake_sleep, fake_now):
    land_pull(tmp_path, "BRA", LE, RID_OLD1)
    land_pull(tmp_path, "BRA", LE, RID_OLD2)
    ingest.ingest_indicator(cfg, LE, http_get=FakeHttpGet(),
                            sleep=fake_sleep, now=fake_now)
    assert len(ingest.find_existing_landing(cfg, LE, "BRA")) == 1


def test_adding_a_country_to_config_is_yaml_only(cfg_dict, load_cfg, tmp_path, fake_sleep, fake_now):
    cfg_dict["countries"].append("ZAF")
    cfg = load_cfg(cfg_dict)
    fake = FakeHttpGet()
    ingest.ingest_indicator(cfg, LE, http_get=fake, sleep=fake_sleep, now=fake_now)
    assert [c for c in dict.fromkeys(k[0] for k in fake.keys)] == ["BRA", "CAN", "ZAF"]
    assert len(files_in(landing_dir(tmp_path, LE))) == 9
