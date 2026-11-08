"""row_count data-quality test and its report (plan sections 9, 10 and the row_count row of section 15)."""
import re

import pandas as pd
import pytest

from helpers import (LE, REPORT_COLS, RID_OLD1, RID_OLD2, RID_OLD3, drop_last_record, files_in, frame_for,
                     land_pull, make_pull, psv_read, reports_dir, write_log, write_report)
from src import dq_tests, staging
from src.dq_tests import RowCountFailure

SMID = "c0a80101-0000-4000-8000-0000000000aa"


def frames_for(tmp_path, specs):
    """specs: [(rid, country, log_total, frame_pull)] -> frames dict + logs written."""
    frames = {}
    for rid, c, log_total, pull in specs:
        write_log(tmp_path, rid, log_total, 3, c, LE)
        frames[rid] = {"country": c, "frame": frame_for(c, LE, rid, pull)}
    return frames


def by_rid(results):
    return {r["request_id"]: r for r in results}


# ----------------------------------------------------------------------------- read_request_log
def test_read_request_log_parses_fields(cfg, tmp_path):
    write_log(tmp_path, RID_OLD1, 7, 3, "BRA", LE)
    assert dq_tests.read_request_log(cfg, LE, RID_OLD1) == {
        "request_id": RID_OLD1, "total": 7, "pages": 3, "country": "BRA", "indicator": LE}


def test_read_request_log_missing_file(cfg):
    with pytest.raises(FileNotFoundError):
        dq_tests.read_request_log(cfg, LE, RID_OLD1)


@pytest.mark.parametrize("line", [
    f"{RID_OLD1}|7|3|BRA",                      # 4 fields
    f"{RID_OLD1}|7|3|BRA|{LE}|extra",           # 6 fields
    f"{RID_OLD1}|seven|3|BRA|{LE}",             # non-int total
    f"{RID_OLD1}|7|x|BRA|{LE}",                 # non-int pages
    f"{RID_OLD2}|7|3|BRA|{LE}",                 # request_id field != file stem
    "garbage",
    "",
])
def test_read_request_log_malformed_raises_value_error(cfg, tmp_path, line):
    p = tmp_path / "log" / LE / f"{RID_OLD1}.log"
    p.parent.mkdir(parents=True)
    p.write_text(line + "\n")
    with pytest.raises(ValueError):
        dq_tests.read_request_log(cfg, LE, RID_OLD1)


# ----------------------------------------------------------------------------- row_count_test
def test_pass_when_log_total_equals_frame_rows(cfg, tmp_path):
    frames = frames_for(tmp_path, [(RID_OLD1, "BRA", 7, None), (RID_OLD2, "CAN", 7, None)])
    results = dq_tests.row_count_test(cfg, LE, frames)
    assert len(results) == 2
    for rid, c in ((RID_OLD1, "BRA"), (RID_OLD2, "CAN")):
        r = by_rid(results)[rid]
        assert r["country_code"] == c and r["indicator_code"] == LE
        assert int(r["expected_total"]) == 7 and int(r["actual_rows"]) == 7
        assert r["status"] == "PASS"
        assert not r["detail"]
    assert dq_tests.assert_row_count_passed(results) is None


def test_result_dict_has_contract_keys(cfg, tmp_path):
    frames = frames_for(tmp_path, [(RID_OLD1, "BRA", 7, None)])
    r = dq_tests.row_count_test(cfg, LE, frames)[0]
    assert {"request_id", "country_code", "indicator_code", "expected_total", "actual_rows",
            "status", "detail"} <= set(r)


def test_null_valued_rows_count(cfg, tmp_path):
    pull = make_pull("BRA", LE, values={2020: None})
    frames = frames_for(tmp_path, [(RID_OLD1, "BRA", 7, pull)])
    assert dq_tests.row_count_test(cfg, LE, frames)[0]["status"] == "PASS"


def test_fail_when_a_record_is_dropped_from_a_page(cfg, tmp_path):
    frames = frames_for(tmp_path, [(RID_OLD1, "BRA", 7, drop_last_record(make_pull("BRA", LE)))])
    r = dq_tests.row_count_test(cfg, LE, frames)[0]
    assert r["status"] == "FAIL"
    assert int(r["expected_total"]) == 7 and int(r["actual_rows"]) == 6
    assert r["detail"]


def test_fail_when_log_total_is_tampered(cfg, tmp_path):
    frames = frames_for(tmp_path, [(RID_OLD1, "BRA", 8, None)])
    r = dq_tests.row_count_test(cfg, LE, frames)[0]
    assert r["status"] == "FAIL" and int(r["expected_total"]) == 8 and int(r["actual_rows"]) == 7


def test_per_country_frames_are_compared_not_the_combined_frame(cfg, tmp_path):
    """BRA has 6 rows (log says 7), CAN has 8 rows (log says 7): the combined total 14 equals 7+7,
    but each request_id must still FAIL."""
    short = drop_last_record(make_pull("BRA", LE))
    long = make_pull("CAN", LE)
    long[-1][1].append(dict(long[-1][1][0], date="2025"))  # an 8th record
    frames = frames_for(tmp_path, [(RID_OLD1, "BRA", 7, short), (RID_OLD2, "CAN", 7, long)])
    assert len(frames[RID_OLD1]["frame"]) + len(frames[RID_OLD2]["frame"]) == 14
    results = by_rid(dq_tests.row_count_test(cfg, LE, frames))
    assert results[RID_OLD1]["status"] == "FAIL" and int(results[RID_OLD1]["actual_rows"]) == 6
    assert results[RID_OLD2]["status"] == "FAIL" and int(results[RID_OLD2]["actual_rows"]) == 8


def test_mixed_pass_and_fail(cfg, tmp_path):
    frames = frames_for(tmp_path, [(RID_OLD1, "BRA", 7, None),
                                   (RID_OLD2, "CAN", 7, drop_last_record(make_pull("CAN", LE)))])
    results = by_rid(dq_tests.row_count_test(cfg, LE, frames))
    assert results[RID_OLD1]["status"] == "PASS" and results[RID_OLD2]["status"] == "FAIL"


def test_missing_log_is_fail_not_crash(cfg, tmp_path):
    frames = {RID_OLD1: {"country": "BRA", "frame": frame_for("BRA", LE, RID_OLD1)}}
    r = dq_tests.row_count_test(cfg, LE, frames)[0]
    assert r["status"] == "FAIL" and r["detail"]


def test_malformed_log_is_fail_not_crash(cfg, tmp_path):
    p = tmp_path / "log" / LE / f"{RID_OLD1}.log"
    p.parent.mkdir(parents=True)
    p.write_text("not|a|valid\n")
    frames = {RID_OLD1: {"country": "BRA", "frame": frame_for("BRA", LE, RID_OLD1)}}
    r = dq_tests.row_count_test(cfg, LE, frames)[0]
    assert r["status"] == "FAIL" and r["detail"]


def test_log_country_differing_from_frame_country_is_fail(cfg, tmp_path):
    write_log(tmp_path, RID_OLD1, 7, 3, "CAN", LE)  # log claims CAN, landing files are BRA
    frames = {RID_OLD1: {"country": "BRA", "frame": frame_for("BRA", LE, RID_OLD1)}}
    r = dq_tests.row_count_test(cfg, LE, frames)[0]
    assert r["status"] == "FAIL" and r["detail"]


# ----------------------------------------------------------------------------- report writing
def results_for(cfg, tmp_path, fail=False):
    second = drop_last_record(make_pull("CAN", LE)) if fail else None
    frames = frames_for(tmp_path, [(RID_OLD1, "BRA", 7, None), (RID_OLD2, "CAN", 7, second)])
    return dq_tests.row_count_test(cfg, LE, frames)


def test_report_path_and_columns(cfg, tmp_path):
    results = results_for(cfg, tmp_path)
    path = dq_tests.write_row_count_report(cfg, LE, results, SMID)
    assert path.parent == reports_dir(tmp_path, LE) == tmp_path / "tests" / LE / "row_count"
    m = re.fullmatch(r"row_count_(\d{8}T\d{12})_" + SMID + r"\.psv", path.name)
    assert m, path.name
    lines = path.read_text().splitlines()
    assert lines[0] == "|".join(REPORT_COLS)
    assert len(lines) == 3
    df = psv_read(path)
    assert set(df["request_id"]) == {RID_OLD1, RID_OLD2}
    assert set(df["status"]) == {"PASS"}
    assert set(df["staging_merge_id"]) == {SMID}
    assert set(df["indicator_code"]) == {LE}
    assert dict(zip(df["request_id"], df["country_code"])) == {RID_OLD1: "BRA", RID_OLD2: "CAN"}
    assert df["expected_total"].tolist() == ["7", "7"] and df["actual_rows"].tolist() == ["7", "7"]
    assert df["checked_at"].nunique() == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", df["checked_at"].iloc[0])
    assert all(line.count("|") == 8 for line in lines)


def test_report_is_written_even_when_failing_and_has_fail_row(cfg, tmp_path):
    results = results_for(cfg, tmp_path, fail=True)
    path = dq_tests.write_row_count_report(cfg, LE, results, SMID)
    df = psv_read(path).set_index("request_id")
    assert df.loc[RID_OLD1, "status"] == "PASS"
    assert df.loc[RID_OLD2, "status"] == "FAIL"
    assert df.loc[RID_OLD2, "expected_total"] == "7" and df.loc[RID_OLD2, "actual_rows"] == "6"
    assert df.loc[RID_OLD2, "detail"] != ""
    assert df.loc[RID_OLD1, "detail"] == ""


def test_successive_reports_have_sortable_names(cfg, tmp_path):
    results = results_for(cfg, tmp_path)
    p1 = dq_tests.write_row_count_report(cfg, LE, results, SMID)
    p2 = dq_tests.write_row_count_report(cfg, LE, results, "d0a80101-0000-4000-8000-0000000000bb")
    ts = lambda p: re.match(r"row_count_(\d{8}T\d{12})_", p.name).group(1)
    assert p1 != p2 and ts(p2) >= ts(p1)
    assert len(files_in(reports_dir(tmp_path, LE))) == 2


def test_report_dir_is_separate_from_pytest_sources(cfg, tmp_path):
    results = results_for(cfg, tmp_path)
    path = dq_tests.write_row_count_report(cfg, LE, results, SMID)
    assert path.suffix == ".psv" and not path.name.startswith("test_")


# ----------------------------------------------------------------------------- assert_row_count_passed
def test_assert_passed_raises_with_failed_rows(cfg, tmp_path):
    results = results_for(cfg, tmp_path, fail=True)
    with pytest.raises(RowCountFailure) as ei:
        dq_tests.assert_row_count_passed(results)
    assert [r["request_id"] for r in ei.value.failed] == [RID_OLD2]
    assert "PipelineError" in [c.__name__ for c in RowCountFailure.__mro__]


def test_assert_passed_none_when_all_pass(cfg, tmp_path):
    assert dq_tests.assert_row_count_passed(results_for(cfg, tmp_path)) is None


def test_assert_passed_lists_every_failure():
    rows = [dict(request_id=RID_OLD1, status="FAIL"), dict(request_id=RID_OLD2, status="PASS"),
            dict(request_id=RID_OLD3, status="FAIL")]
    with pytest.raises(RowCountFailure) as ei:
        dq_tests.assert_row_count_passed(rows)
    assert [r["request_id"] for r in ei.value.failed] == [RID_OLD1, RID_OLD3]


# ----------------------------------------------------------------------------- latest_report
def test_latest_report_none_when_no_reports(cfg, tmp_path):
    assert dq_tests.latest_report(cfg, LE) is None
    reports_dir(tmp_path, LE).mkdir(parents=True)
    assert dq_tests.latest_report(cfg, LE) is None


def test_latest_report_is_greatest_filename_not_newest_mtime(cfg, tmp_path):
    newer = write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 7, "PASS")], ts="20261108T170000000000",
                         smid="aaaaaaaa-0000-4000-8000-000000000002")
    older = write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 7, "PASS")], ts="20261108T150000000000",
                         smid="aaaaaaaa-0000-4000-8000-000000000001")  # written last -> newest mtime
    assert dq_tests.latest_report(cfg, LE) == newer


def test_latest_report_ignores_marker_and_other_files(cfg, tmp_path):
    newer = write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 7, "PASS")], ts="20261108T170000000000",
                         smid="aaaaaaaa-0000-4000-8000-000000000002")
    write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 7, "PASS")], ts="20261108T150000000000",
                 smid="aaaaaaaa-0000-4000-8000-000000000001")
    d = reports_dir(tmp_path, LE)
    (d / "staging_failed_request_ids.txt").write_text(RID_OLD1 + "\n")
    (d / "zzz_notes.psv").write_text("x\n")
    (d / "row_count_notes.txt").write_text("x\n")
    assert dq_tests.latest_report(cfg, LE) == newer


# ----------------------------------------------------------------------------- integration with staging
def test_row_count_on_real_staging_frames_pass_and_fail(cfg, tmp_path):
    land_pull(tmp_path, "BRA", LE, RID_OLD1)
    land_pull(tmp_path, "CAN", LE, RID_OLD2, pull=drop_last_record(make_pull("CAN", LE)), with_log=False)
    write_log(tmp_path, RID_OLD2, 7, 3, "CAN", LE)  # the pull's log says 7, landing holds 6
    _, frames, smid, _ = staging.build_staging(cfg, LE)
    results = by_rid(dq_tests.row_count_test(cfg, LE, frames))
    assert results[RID_OLD1]["status"] == "PASS"
    assert results[RID_OLD2]["status"] == "FAIL"
    path = dq_tests.write_row_count_report(cfg, LE, list(results.values()), smid)
    assert path.name.endswith(f"_{smid}.psv")
    with pytest.raises(RowCountFailure):
        dq_tests.assert_row_count_passed(list(results.values()))
