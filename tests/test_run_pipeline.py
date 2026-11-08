"""run_indicator / run_all / main: failure semantics, exit codes, CLI, end-to-end with mocked API
(plan sections 9, 14, 16 and the End-to-end row of section 15)."""
import json
import re

import pytest
import requests

from helpers import (BASE_COLS, GDP, LE, RID_OLD1, STAGING_COLS, FakeHttpGet, FakeNow, FakeResponse, FakeSleep,
                     analysis_out, archived_dir, base_path, base_cfg_dict, drop_last_record, files_in, land_pull,
                     landing_dir, log_path, make_pull, marker_path, psv_read, psv_write, rejected_dir, reports_dir,
                     rid_at, stg_df, staging_path, write_cfg, write_log)
from src import run_pipeline
from src.merge import MergeError
from src.staging import StagingError
from src.dq_tests import RowCountFailure


def seams(api, sleep, now):
    return dict(http_get=api, sleep=sleep, now=now)


def count_files(root, ind, kind):
    d = {"landing": landing_dir, "archived": archived_dir, "rejected": rejected_dir}[kind](root, ind)
    return len(files_in(d))


def reports(root, ind):
    return sorted(files_in(reports_dir(root, ind)))


def biz_part(path):
    return [l.split("|")[:10] for l in path.read_text().splitlines()]


# ============================================================================= run_indicator
def test_run_indicator_happy_path(cfg, tmp_path, fake_sleep, fake_now):
    api = FakeHttpGet()
    summary = run_pipeline.run_indicator(cfg, LE, **seams(api, fake_sleep, fake_now))
    assert isinstance(summary, dict)
    assert len(api.keys) == 6 and {k[1] for k in api.keys} == {LE}
    assert count_files(tmp_path, LE, "landing") == 0               # landing empty after archive
    assert count_files(tmp_path, LE, "archived") == 6
    assert count_files(tmp_path, LE, "rejected") == 0
    assert len(files_in(tmp_path / "log" / LE)) == 2
    sp = staging_path(tmp_path, LE)
    assert sp.read_text().splitlines()[0] == "|".join(STAGING_COLS)
    assert len(psv_read(sp)) == 14
    rep = reports(tmp_path, LE)
    assert len(rep) == 1
    r = psv_read(reports_dir(tmp_path, LE) / rep[0])
    assert r["status"].tolist() == ["PASS", "PASS"]
    base = psv_read(base_path(tmp_path, LE))
    assert list(base.columns) == BASE_COLS and len(base) == 14
    assert set(base["staging_merge_id"]) == set(psv_read(sp)["staging_merge_id"])
    assert fake_sleep.calls == []
    assert not base_path(tmp_path, GDP).exists()


def test_row_count_failure_halts_before_archive_and_merge(cfg, tmp_path, fake_sleep, fake_now):
    api = FakeHttpGet(pulls={("CAN", LE): drop_last_record(make_pull("CAN", LE))})
    with pytest.raises(RowCountFailure) as ei:
        run_pipeline.run_indicator(cfg, LE, **seams(api, fake_sleep, fake_now))
    assert [r["country_code"] for r in ei.value.failed] == ["CAN"]
    assert count_files(tmp_path, LE, "landing") == 6              # landing files stay for rerun
    assert count_files(tmp_path, LE, "archived") == 0
    assert not base_path(tmp_path, LE).exists()
    assert staging_path(tmp_path, LE).exists()                    # staging file is kept
    rep = psv_read(reports_dir(tmp_path, LE) / reports(tmp_path, LE)[0]).set_index("country_code")
    assert rep.loc["BRA", "status"] == "PASS" and rep.loc["CAN", "status"] == "FAIL"
    assert rep.loc["CAN", "expected_total"] == "7" and rep.loc["CAN", "actual_rows"] == "6"


def test_row_count_failure_leaves_existing_base_untouched(cfg, tmp_path, fake_sleep, fake_now):
    run_pipeline.run_indicator(cfg, LE, **seams(FakeHttpGet(), fake_sleep, fake_now))
    bp = base_path(tmp_path, LE)
    before = bp.read_bytes()
    api = FakeHttpGet(pulls={("BRA", LE): drop_last_record(make_pull("BRA", LE))})
    with pytest.raises(RowCountFailure):
        run_pipeline.run_indicator(cfg, LE, **seams(api, fake_sleep, fake_now))
    assert bp.read_bytes() == before
    assert count_files(tmp_path, LE, "landing") == 6
    assert count_files(tmp_path, LE, "archived") == 6             # only the first run's files


def test_rerun_after_row_count_failure_repulls_only_the_failed_country(cfg, tmp_path, fake_sleep, fake_now):
    bad = FakeHttpGet(pulls={("CAN", LE): drop_last_record(make_pull("CAN", LE))})
    with pytest.raises(RowCountFailure):
        run_pipeline.run_indicator(cfg, LE, **seams(bad, fake_sleep, fake_now))
    old_can = [n for n in files_in(landing_dir(tmp_path, LE)) if n.startswith("CAN_")]
    good = FakeHttpGet()
    run_pipeline.run_indicator(cfg, LE, **seams(good, fake_sleep, fake_now))
    assert good.keys == [("CAN", LE, 1), ("CAN", LE, 2), ("CAN", LE, 3)]
    assert files_in(rejected_dir(tmp_path, LE)) == sorted(old_can)
    assert count_files(tmp_path, LE, "landing") == 0 and count_files(tmp_path, LE, "archived") == 6
    assert len(psv_read(base_path(tmp_path, LE))) == 14


def test_refetch_repulls_everything(cfg, tmp_path, fake_sleep, fake_now):
    bad = FakeHttpGet(pulls={("CAN", LE): drop_last_record(make_pull("CAN", LE))})
    with pytest.raises(RowCountFailure):
        run_pipeline.run_indicator(cfg, LE, **seams(bad, fake_sleep, fake_now))
    good = FakeHttpGet()
    run_pipeline.run_indicator(cfg, LE, refetch=True, **seams(good, fake_sleep, fake_now))
    assert len(good.keys) == 6
    assert count_files(tmp_path, LE, "rejected") == 6
    assert count_files(tmp_path, LE, "archived") == 6


def test_staging_error_marks_request_id_and_next_run_repulls_only_that_pair(cfg, tmp_path, fake_sleep, fake_now):
    bad = make_pull("BRA", LE)
    bad[2][1][0]["date"] = "2017"                                   # outside the configured range
    old = land_pull(tmp_path, "BRA", LE, RID_OLD1, bad)
    api = FakeHttpGet()
    with pytest.raises(StagingError) as ei:
        run_pipeline.run_indicator(cfg, LE, **seams(api, fake_sleep, fake_now))
    assert set(ei.value.request_ids) == {RID_OLD1}
    assert {k[0] for k in api.keys} == {"CAN"}                      # BRA's leftover group was resumed, not re-pulled
    assert marker_path(tmp_path, LE).read_text().splitlines() == [RID_OLD1]
    assert not base_path(tmp_path, LE).exists() and count_files(tmp_path, LE, "archived") == 0

    api2 = FakeHttpGet()
    run_pipeline.run_indicator(cfg, LE, **seams(api2, fake_sleep, fake_now))
    assert api2.keys == [("BRA", LE, 1), ("BRA", LE, 2), ("BRA", LE, 3)]
    assert files_in(rejected_dir(tmp_path, LE)) == sorted(p.name for p in old)
    assert not marker_path(tmp_path, LE).exists()
    assert len(psv_read(base_path(tmp_path, LE))) == 14


def test_second_run_keeps_business_data_and_extends_lineage(cfg, tmp_path, fake_sleep, fake_now):
    run_pipeline.run_indicator(cfg, LE, **seams(FakeHttpGet(), fake_sleep, fake_now))
    bp = base_path(tmp_path, LE)
    first = psv_read(bp)
    run_pipeline.run_indicator(cfg, LE, **seams(FakeHttpGet(), fake_sleep, fake_now))
    second = psv_read(bp)
    assert second[second.columns[:10]].values.tolist() == first[first.columns[:10]].values.tolist()
    assert all(len(x.split(";")) == 2 for x in second["request_id_linage"])
    assert all(len(x.split(";")) == 2 for x in second["staging_merged_id_linage"])
    assert set(second["request_id"]).isdisjoint(set(first["request_id"]))
    assert set(second["staging_merge_id"]).isdisjoint(set(first["staging_merge_id"]))
    assert all(a.split(";")[0] == b for a, b in zip(second["request_id_linage"], first["request_id"]))


# ============================================================================= run_all
def test_run_all_runs_every_indicator_in_config_order_then_analysis(cfg, tmp_path, real_sql, fake_sleep, fake_now):
    api = FakeHttpGet()
    run_pipeline.run_all(cfg, **seams(api, fake_sleep, fake_now))
    inds = list(dict.fromkeys(k[1] for k in api.keys))
    assert inds == [LE, GDP]
    assert len(api.keys) == 12
    assert base_path(tmp_path, LE).exists() and base_path(tmp_path, GDP).exists()
    assert analysis_out(tmp_path).exists()


def test_run_all_indicator_subset(cfg, tmp_path, fake_sleep, fake_now):
    api = FakeHttpGet()
    run_pipeline.run_all(cfg, indicators=[GDP], skip_analysis=True, **seams(api, fake_sleep, fake_now))
    assert {k[1] for k in api.keys} == {GDP}
    assert base_path(tmp_path, GDP).exists() and not base_path(tmp_path, LE).exists()


def test_run_all_skip_analysis(cfg, tmp_path, real_sql, fake_sleep, fake_now):
    run_pipeline.run_all(cfg, skip_analysis=True, **seams(FakeHttpGet(), fake_sleep, fake_now))
    assert base_path(tmp_path, GDP).exists() and not analysis_out(tmp_path).exists()


def test_failure_in_first_indicator_stops_everything(cfg, tmp_path, real_sql, fake_sleep, fake_now):
    api = FakeHttpGet(pulls={("BRA", LE): drop_last_record(make_pull("BRA", LE))})
    with pytest.raises(RowCountFailure):
        run_pipeline.run_all(cfg, **seams(api, fake_sleep, fake_now))
    assert GDP not in {k[1] for k in api.keys}
    assert not base_path(tmp_path, GDP).exists() and not analysis_out(tmp_path).exists()


def test_failure_in_second_indicator_keeps_first_merged_and_skips_analysis(cfg, tmp_path, real_sql, fake_sleep, fake_now):
    api = FakeHttpGet(pulls={("BRA", GDP): drop_last_record(make_pull("BRA", GDP))})
    with pytest.raises(RowCountFailure):
        run_pipeline.run_all(cfg, **seams(api, fake_sleep, fake_now))
    assert base_path(tmp_path, LE).exists() and not base_path(tmp_path, GDP).exists()
    assert not analysis_out(tmp_path).exists()


# ============================================================================= main: arguments and seams
@pytest.fixture
def captured(monkeypatch):
    box = {}

    def fake_run_all(cfg, **kw):
        box["cfg"], box["kw"] = cfg, kw

    monkeypatch.setattr(run_pipeline, "run_all", fake_run_all)
    return box


def call_main(cfg_path, *extra, api=None, sleep=None, now=None):
    return run_pipeline.main(["--config", str(cfg_path), *extra], http_get=api or FakeHttpGet(auto=False),
                             sleep=sleep or FakeSleep(), now=now or FakeNow())


def test_main_defaults(cfg_path, captured):
    assert call_main(cfg_path) == 0
    kw = captured["kw"]
    assert kw["indicators"] is None
    assert kw["skip_analysis"] is False and kw["refetch"] is False
    assert captured["cfg"]["countries"] == ["BRA", "CAN"]
    assert captured["cfg"]["_root"].resolve() == cfg_path.parent.parent.resolve()


def test_main_flags(cfg_path, captured):
    assert call_main(cfg_path, "--indicator", LE, GDP, "--skip-analysis", "--refetch", "--log-level", "DEBUG") == 0
    kw = captured["kw"]
    assert list(kw["indicators"]) == [LE, GDP]
    assert kw["skip_analysis"] is True and kw["refetch"] is True


def test_main_single_indicator(cfg_path, captured):
    assert call_main(cfg_path, "--indicator", GDP) == 0
    assert list(captured["kw"]["indicators"]) == [GDP]


def test_main_forwards_the_seams_to_run_all(cfg_path, captured):
    api, sleep, now = FakeHttpGet(auto=False), FakeSleep(), FakeNow()
    assert call_main(cfg_path, api=api, sleep=sleep, now=now) == 0
    assert captured["kw"]["http_get"] is api
    assert captured["kw"]["sleep"] is sleep
    assert captured["kw"]["now"] is now


def test_main_works_from_any_working_directory(cfg_path, captured, tmp_path, monkeypatch):
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert call_main(cfg_path) == 0
    assert captured["cfg"]["_root"].resolve() == cfg_path.parent.parent.resolve()


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyError("k"), ZeroDivisionError()])
def test_unexpected_exception_exits_1(cfg_path, monkeypatch, exc):
    def boom(cfg, **kw):
        raise exc
    monkeypatch.setattr(run_pipeline, "run_all", boom)
    assert call_main(cfg_path) == 1


# ============================================================================= main: exit codes through the real flow
def test_exit_2_config_error(tmp_path, fake_sleep, fake_now):
    d = base_cfg_dict()
    d["date_range"] = {"start": 2025, "end": 2018}
    path = write_cfg(tmp_path, d)
    api = FakeHttpGet(auto=False)
    assert call_main(path, api=api, sleep=fake_sleep, now=fake_now) == 2
    assert api.calls == []


def test_exit_3_rate_limit_exhausted_with_clear_message(cfg_path, tmp_path, fake_sleep, fake_now, capsys):
    r429 = FakeResponse(429, None, text="slow")
    api = FakeHttpGet(responses={("BRA", LE, 1): (r429,) * 4}, auto=False)
    assert call_main(cfg_path, api=api, sleep=fake_sleep, now=fake_now) == 3
    assert len(api.calls) == 4 and fake_sleep.calls == [60, 60, 60]
    err = capsys.readouterr().err
    assert "429" in err and "BRA" in err
    assert files_in(landing_dir(tmp_path, LE)) == []


@pytest.mark.parametrize("item", [FakeResponse(503, None, text="x"), requests.ConnectionError("down"),
                                  FakeResponse(404, None, text="nf"), FakeResponse(200, FakeResponse.NOT_JSON)])
def test_exit_3_other_api_failures(cfg_path, fake_sleep, fake_now, item):
    seq = (item,) * 4
    api = FakeHttpGet(responses={("BRA", LE, 1): seq}, auto=False)
    assert call_main(cfg_path, api=api, sleep=fake_sleep, now=fake_now) == 3


def test_exit_4_schema_error_stops_that_pull(cfg_path, tmp_path, fake_sleep, fake_now):
    bad = make_pull("BRA", LE)
    bad[0][1][0]["indicator"]["id"] = "WRONG.CODE"
    api = FakeHttpGet(pulls={("BRA", LE): bad})
    assert call_main(cfg_path, api=api, sleep=fake_sleep, now=fake_now) == 4
    assert files_in(landing_dir(tmp_path, LE)) == [] and not base_path(tmp_path, LE).exists()


def test_exit_5_row_count_failure(cfg_path, tmp_path, fake_sleep, fake_now):
    api = FakeHttpGet(pulls={("BRA", LE): drop_last_record(make_pull("BRA", LE))})
    assert call_main(cfg_path, api=api, sleep=fake_sleep, now=fake_now) == 5
    assert count_files(tmp_path, LE, "landing") == 6 and not base_path(tmp_path, LE).exists()
    assert GDP not in {k[1] for k in api.keys}


def test_exit_6_staging_error(cfg_path, tmp_path, fake_sleep, fake_now):
    bad = make_pull("BRA", LE)
    bad[2][1][0]["date"] = "2017"
    land_pull(tmp_path, "BRA", LE, RID_OLD1, bad)
    assert call_main(cfg_path, api=FakeHttpGet(), sleep=fake_sleep, now=fake_now) == 6
    assert marker_path(tmp_path, LE).read_text().splitlines() == [RID_OLD1]


def test_exit_7_merge_error_leaves_base_untouched(cfg_path, tmp_path, fake_sleep, fake_now):
    bp = base_path(tmp_path, LE)
    bp.parent.mkdir(parents=True)
    bp.write_text("countryiso3code|date|value\nBRA|2018|1\n")        # base with drifted columns
    before = bp.read_bytes()
    assert call_main(cfg_path, "--indicator", LE, api=FakeHttpGet(), sleep=fake_sleep, now=fake_now) == 7
    assert bp.read_bytes() == before


def test_exit_8_file_collision(cfg_path, tmp_path, fake_sleep, fake_now):
    existing = write_log(tmp_path, rid_at(0), 99, 99, "XXX", LE)    # the id the first pull will get
    assert call_main(cfg_path, api=FakeHttpGet(), sleep=fake_sleep, now=fake_now) == 8
    assert existing.read_text() == f"{rid_at(0)}|99|99|XXX|{LE}\n"


def test_exit_9_analysis_error_when_a_base_table_is_missing(cfg_path, tmp_path, real_sql, fake_sleep, fake_now):
    api = FakeHttpGet()
    assert call_main(cfg_path, "--indicator", LE, api=api, sleep=fake_sleep, now=fake_now) == 9
    assert base_path(tmp_path, LE).exists() and not base_path(tmp_path, GDP).exists()
    assert not analysis_out(tmp_path).exists()


def test_subset_with_skip_analysis_succeeds(cfg_path, tmp_path, fake_sleep, fake_now):
    api = FakeHttpGet()
    assert call_main(cfg_path, "--indicator", LE, "--skip-analysis", api=api, sleep=fake_sleep, now=fake_now) == 0
    assert len(api.keys) == 6 and base_path(tmp_path, LE).exists()


def test_unexpected_error_in_http_layer_exits_1(cfg_path, fake_sleep, fake_now):
    def broken(url, timeout=None):
        raise RuntimeError("not a requests error")
    assert call_main(cfg_path, api=broken, sleep=fake_sleep, now=fake_now) == 1


def test_resume_after_later_indicator_failure_with_indicator_flag(cfg_path, tmp_path, real_sql, fake_sleep, fake_now):
    bad = FakeHttpGet(pulls={("CAN", GDP): drop_last_record(make_pull("CAN", GDP))})
    assert call_main(cfg_path, api=bad, sleep=fake_sleep, now=fake_now) == 5
    le_base = base_path(tmp_path, LE).read_bytes()
    good = FakeHttpGet()
    assert call_main(cfg_path, "--indicator", GDP, api=good, sleep=fake_sleep, now=fake_now) == 0
    assert {k[1] for k in good.keys} == {GDP} and {k[0] for k in good.keys} == {"CAN"}
    assert base_path(tmp_path, LE).read_bytes() == le_base          # LE not re-pulled, not re-merged
    assert analysis_out(tmp_path).exists()


# ============================================================================= end to end, mocked
def test_end_to_end_all_artifacts(cfg_path, tmp_path, real_sql, fake_sleep, fake_now):
    api = FakeHttpGet()
    assert call_main(cfg_path, api=api, sleep=fake_sleep, now=fake_now) == 0
    assert len(api.calls) == 12                       # 2 countries x 2 indicators x 3 pages
    assert fake_sleep.calls == []
    assert fake_now.count == 4                         # one request_id per pull
    for ind in (LE, GDP):
        assert count_files(tmp_path, ind, "landing") == 0
        assert count_files(tmp_path, ind, "archived") == 6
        logs = files_in(tmp_path / "log" / ind)
        assert len(logs) == 2
        for name in logs:
            rid = name[:-4]
            assert re.fullmatch(r"\d{8}T\d{12}", rid)
            line = (tmp_path / "log" / ind / name).read_text()
            assert re.fullmatch(rf"{rid}\|7\|3\|(BRA|CAN)\|{re.escape(ind)}\n", line)
        assert staging_path(tmp_path, ind).exists()
        base = psv_read(base_path(tmp_path, ind))
        assert list(base.columns) == BASE_COLS and len(base) == 14
        assert set(base["countryiso3code"]) == {"BRA", "CAN"}
        rep = reports(tmp_path, ind)
        assert len(rep) == 1 and set(psv_read(reports_dir(tmp_path, ind) / rep[0])["status"]) == {"PASS"}
    out = analysis_out(tmp_path)
    assert psv_read(out)["countryiso3code"].tolist() == ["BRA", "CAN"]
    assert (tmp_path / "log" / "pipeline.log").exists()
    assert (tmp_path / "sql" / "code" / "analysis.sql").exists()


def test_end_to_end_second_run_is_idempotent_for_business_data(cfg_path, tmp_path, real_sql, fake_sleep, fake_now):
    assert call_main(cfg_path, api=FakeHttpGet(), sleep=fake_sleep, now=fake_now) == 0
    before = {ind: base_path(tmp_path, ind).read_text() for ind in (LE, GDP)}
    analysis_before = analysis_out(tmp_path).read_bytes()
    api2 = FakeHttpGet()
    assert call_main(cfg_path, api=api2, sleep=fake_sleep, now=fake_now) == 0
    assert len(api2.calls) == 12
    for ind in (LE, GDP):
        a = psv_read(base_path(tmp_path, ind))
        first_lines = before[ind].splitlines()
        assert [l.split("|")[:10] for l in base_path(tmp_path, ind).read_text().splitlines()] == \
               [l.split("|")[:10] for l in first_lines]
        assert all(len(x.split(";")) == 2 for x in a["request_id_linage"])
        assert all(len(x.split(";")) == 2 for x in a["staging_merged_id_linage"])
        assert (a["request_id"] != [l.split("|")[10] for l in first_lines[1:]]).all()
        assert count_files(tmp_path, ind, "archived") == 12
        assert len(files_in(tmp_path / "log" / ind)) == 4
        assert len(reports(tmp_path, ind)) == 2
    assert analysis_out(tmp_path).read_bytes() == analysis_before


def test_end_to_end_three_runs_lineage_length_three(cfg_path, tmp_path, real_sql, fake_sleep, fake_now):
    for _ in range(3):
        assert call_main(cfg_path, api=FakeHttpGet(), sleep=fake_sleep, now=fake_now) == 0
    base = psv_read(base_path(tmp_path, LE))
    assert all(len(x.split(";")) == 3 for x in base["request_id_linage"])
    assert all(x.split(";")[-1] == r for x, r in zip(base["request_id_linage"], base["request_id"]))
    assert all(x.split(";")[-1] == s for x, s in zip(base["staging_merged_id_linage"], base["staging_merge_id"]))


def test_end_to_end_adding_country_and_indicator_is_yaml_only(tmp_path, real_sql, fake_sleep, fake_now):
    d = base_cfg_dict()
    d["countries"].append("ZAF")
    d["indicators"].append({"code": "SP.POP.TOTL", "name": "Population, total", "table_name": "population_total",
                            "primary_key": ["countryiso3code", "date"]})
    path = write_cfg(tmp_path, d)
    api = FakeHttpGet()
    assert call_main(path, api=api, sleep=fake_sleep, now=fake_now) == 0
    assert len(api.calls) == 3 * 3 * 3
    assert len(psv_read(base_path(tmp_path, "SP.POP.TOTL"))) == 21
    assert len(psv_read(base_path(tmp_path, LE))) == 21
    assert psv_read(analysis_out(tmp_path))["countryiso3code"].tolist() == ["BRA", "CAN", "ZAF"]


def test_end_to_end_with_429_and_503_recovers_via_injected_sleep(cfg_path, tmp_path, real_sql, fake_sleep, fake_now):
    p = make_pull("BRA", LE)
    r429, r503 = FakeResponse(429, None, text="x"), FakeResponse(503, None, text="x")
    api = FakeHttpGet(responses={("BRA", LE, 2): (r429, r503, p[1])})
    assert call_main(cfg_path, api=api, sleep=fake_sleep, now=fake_now) == 0
    assert fake_sleep.calls == [60, 60]
    assert len(api.calls) == 14
