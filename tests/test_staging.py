"""Staging: landing -> per-country frames -> combined staging table + staging_merge_id
(plan sections 5, 10 and the Staging / Staging-failure marker rows of section 15)."""
import copy
import json
import uuid

import pandas as pd
import pytest

from helpers import (BIZ_COLS, FRAME_COLS, GDP, LE, RID_OLD1, RID_OLD2, RID_OLD3, STAGING_COLS, files_in,
                     frame_for, frames_equal, land_pull, landing_dir, make_page, make_pull, page_name, psv_read, staging_path)
from src import staging
from src.schema_check import SchemaCheckError
from src.staging import StagingError


def err_ids(e):
    return set(e.request_ids)


# ----------------------------------------------------------------------------- flatten_records
def test_flatten_records_columns_order_and_row_count():
    pull = make_pull("BRA", LE)
    df = staging.flatten_records(pull, RID_OLD1)
    assert list(df.columns) == FRAME_COLS
    assert len(df) == 7
    assert df["request_id"].tolist() == [RID_OLD1] * 7
    assert df["countryiso3code"].tolist() == ["BRA"] * 7
    # pages concatenated in page order
    assert df["date"].tolist() == [r["date"] for pg in pull for r in pg[1]]


def test_flatten_records_maps_nested_fields_and_all_columns_are_strings():
    pull = make_pull("BRA", LE, per_page=50)
    df = staging.flatten_records(pull, RID_OLD1)
    row = df.iloc[0]
    rec = pull[0][1][0]
    assert row["indicator_id"] == LE and row["indicator_name"] == rec["indicator"]["value"]
    assert row["country_id"] == "BR" and row["country_name"] == "Brazil"
    assert row["unit"] == "" and row["obs_status"] == "" and row["decimal"] == "0"
    assert all(isinstance(v, str) for v in df.to_numpy().ravel())


def test_flatten_records_string_conversion_is_pinned():
    values = {2024: 212812405, 2023: None, 2022: 2185821610689.31, 2021: 70.5, 2020: 1.5e16,
              2019: 0, 2018: 83}
    df = staging.flatten_records(make_pull("BRA", LE, per_page=50, values=values), RID_OLD1)
    got = dict(zip(df["date"], df["value"]))
    assert got["2024"] == "212812405"          # int -> str(int), never 212812405.0
    assert got["2023"] == ""                   # None -> ""
    assert got["2022"] == "2185821610689.31"   # float -> repr
    assert got["2021"] == "70.5"
    assert got["2020"] == repr(1.5e16)
    assert got["2019"] == "0" and got["2018"] == "83"


def test_flatten_records_int_column_with_nulls_does_not_become_float():
    values = {2024: 212812405, 2023: None, 2022: 212000001}
    df = staging.flatten_records(make_pull("BRA", LE, years=[2022, 2023, 2024], per_page=50, values=values),
                                 RID_OLD1)
    assert sorted(df["value"]) == ["", "212000001", "212812405"]


def test_flatten_records_keeps_string_fields_unchanged():
    pull = make_pull("BRA", LE, per_page=50)
    pull[0][1][0]["unit"] = "years"
    pull[0][1][0]["obs_status"] = "E"
    pull[0][1][0]["decimal"] = 1
    df = staging.flatten_records(pull, RID_OLD1)
    assert df.iloc[0]["unit"] == "years" and df.iloc[0]["obs_status"] == "E" and df.iloc[0]["decimal"] == "1"


# ----------------------------------------------------------------------------- read_landing_file
def test_read_landing_file_parses_name_and_content(tmp_path):
    pull = make_pull("BRA", LE)
    paths = land_pull(tmp_path, "BRA", LE, RID_OLD1, pull)
    assert staging.read_landing_file(paths[1]) == ("BRA", LE, RID_OLD1, 2, pull[1])


def test_read_landing_file_invalid_json_raises_staging_error(tmp_path):
    p = tmp_path / page_name("BRA", LE, RID_OLD1, 1)
    p.write_text("{not json")
    with pytest.raises(StagingError):
        staging.read_landing_file(p)


@pytest.mark.parametrize("content", ['{"a": 1}', "[]", '[{"page": 1}]', "[1, 2, 3]", '"text"', "null"])
def test_read_landing_file_requires_two_element_list(tmp_path, content):
    p = tmp_path / page_name("BRA", LE, RID_OLD1, 1)
    p.write_text(content)
    with pytest.raises(StagingError):
        staging.read_landing_file(p)


def test_read_landing_file_country_mismatch_between_name_and_content(tmp_path):
    p = tmp_path / page_name("CAN", LE, RID_OLD1, 1)
    p.write_text(json.dumps(make_pull("BRA", LE)[0]))
    with pytest.raises(StagingError):
        staging.read_landing_file(p)


def test_read_landing_file_indicator_mismatch_between_name_and_content(tmp_path):
    p = tmp_path / page_name("BRA", GDP, RID_OLD1, 1)
    p.write_text(json.dumps(make_pull("BRA", LE)[0]))
    with pytest.raises(StagingError):
        staging.read_landing_file(p)


# ----------------------------------------------------------------------------- build_country_frames
def test_build_country_frames_one_frame_per_request_id_with_all_pages(cfg, tmp_path):
    land_pull(tmp_path, "BRA", LE, RID_OLD1)
    land_pull(tmp_path, "CAN", LE, RID_OLD2)
    frames = staging.build_country_frames(cfg, LE)
    assert set(frames) == {RID_OLD1, RID_OLD2}
    assert frames[RID_OLD1]["country"] == "BRA" and frames[RID_OLD2]["country"] == "CAN"
    for rid, c in ((RID_OLD1, "BRA"), (RID_OLD2, "CAN")):
        df = frames[rid]["frame"]
        assert len(df) == 7  # 3 + 3 + 1 records from the three page files
        assert list(df.columns) == FRAME_COLS
        assert set(df["countryiso3code"]) == {c}
        assert set(df["request_id"]) == {rid}
    assert frames_equal(frames[RID_OLD1]["frame"], frame_for("BRA", LE, RID_OLD1))


def test_build_country_frames_only_reads_the_requested_indicator(cfg, tmp_path):
    land_pull(tmp_path, "BRA", LE, RID_OLD1)
    land_pull(tmp_path, "BRA", GDP, RID_OLD2)
    frames = staging.build_country_frames(cfg, LE)
    assert set(frames) == {RID_OLD1}


def test_build_country_frames_null_values_count_as_rows(cfg, tmp_path):
    land_pull(tmp_path, "BRA", LE, RID_OLD1, make_pull("BRA", LE, values={2020: None, 2021: None}))
    df = staging.build_country_frames(cfg, LE)[RID_OLD1]["frame"]
    assert len(df) == 7 and (df["value"] == "").sum() == 2


@pytest.mark.parametrize("name, mutate", [
    ("missing_middle_page", lambda paths: paths[1].unlink()),
    ("missing_last_page", lambda paths: paths[2].unlink()),
    ("missing_first_page", lambda paths: paths[0].unlink()),
])
def test_incomplete_page_set_raises_staging_error_with_request_id(cfg, tmp_path, name, mutate):
    paths = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    mutate(paths)
    with pytest.raises(StagingError) as ei:
        staging.build_country_frames(cfg, LE)
    assert err_ids(ei.value) == {RID_OLD1}
    assert not isinstance(ei.value, SchemaCheckError)


def test_extra_page_number_beyond_pages_raises(cfg, tmp_path):
    paths = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    (landing_dir(tmp_path, LE) / page_name("BRA", LE, RID_OLD1, 4)).write_text(paths[2].read_text())
    with pytest.raises(StagingError) as ei:
        staging.build_country_frames(cfg, LE)
    assert err_ids(ei.value) == {RID_OLD1}


def test_files_disagreeing_on_total_or_pages_raise(cfg, tmp_path):
    pull = make_pull("BRA", LE)
    pull[2][0]["total"] = 8
    land_pull(tmp_path, "BRA", LE, RID_OLD1, pull)
    with pytest.raises(StagingError) as ei:
        staging.build_country_frames(cfg, LE)
    assert err_ids(ei.value) == {RID_OLD1}


def test_file_name_page_differing_from_metadata_page_raises(cfg, tmp_path):
    pull = make_pull("BRA", LE)
    pull[1], pull[2] = pull[2], pull[1]  # file p2 holds the page whose metadata says 3
    land_pull(tmp_path, "BRA", LE, RID_OLD1, pull)
    with pytest.raises(StagingError) as ei:
        staging.build_country_frames(cfg, LE)
    assert err_ids(ei.value) == {RID_OLD1}


def test_leftover_file_with_date_outside_range_raises_staging_error_not_schema_error(cfg, tmp_path):
    pull = make_pull("BRA", LE)
    pull[2][1][0]["date"] = "2017"
    land_pull(tmp_path, "BRA", LE, RID_OLD1, pull)
    land_pull(tmp_path, "CAN", LE, RID_OLD2)
    with pytest.raises(StagingError) as ei:
        staging.build_country_frames(cfg, LE)
    assert type(ei.value) is StagingError
    assert err_ids(ei.value) == {RID_OLD1}


def test_recheck_uses_current_config_date_range(cfg_dict, load_cfg, tmp_path):
    land_pull(tmp_path, "BRA", LE, RID_OLD1)
    cfg_dict["date_range"] = {"start": 2020, "end": 2024}  # 2018/2019 now out of range
    cfg = load_cfg(cfg_dict)
    with pytest.raises(StagingError) as ei:
        staging.build_country_frames(cfg, LE)
    assert err_ids(ei.value) == {RID_OLD1}


def test_recheck_checks_both_array_elements(cfg, tmp_path):
    pull = make_pull("BRA", LE)
    del pull[1][0]["sourceid"]
    land_pull(tmp_path, "BRA", LE, RID_OLD1, pull)
    with pytest.raises(StagingError) as ei:
        staging.build_country_frames(cfg, LE)
    assert err_ids(ei.value) == {RID_OLD1}


def test_unreadable_landing_file_raises_staging_error_with_request_id(cfg, tmp_path):
    paths = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    paths[0].write_text("{broken")
    with pytest.raises(StagingError) as ei:
        staging.build_country_frames(cfg, LE)
    assert err_ids(ei.value) == {RID_OLD1}


def test_two_request_ids_for_one_pair_raise(cfg, tmp_path):
    land_pull(tmp_path, "BRA", LE, RID_OLD1)
    land_pull(tmp_path, "BRA", LE, RID_OLD2)
    with pytest.raises(StagingError):
        staging.build_country_frames(cfg, LE)


# ----------------------------------------------------------------------------- combine_frames
def frames_dict(*items):
    return {rid: {"country": c, "frame": frame_for(c, LE, rid)} for rid, c in items}


def test_combine_frames_stacks_and_sorts_by_pk():
    frames = frames_dict((RID_OLD2, "CAN"), (RID_OLD1, "BRA"))
    out = staging.combine_frames(frames, ["countryiso3code", "date"])
    assert len(out) == 14
    assert list(out.columns) == FRAME_COLS
    keys = list(zip(out["countryiso3code"], out["date"]))
    assert keys == sorted(keys)
    assert keys[0] == ("BRA", "2018") and keys[-1] == ("CAN", "2024")


def test_combine_frames_does_not_alter_input_frames():
    frames = frames_dict((RID_OLD1, "BRA"), (RID_OLD2, "CAN"))
    before = {k: v["frame"].copy() for k, v in frames.items()}
    staging.combine_frames(frames, ["countryiso3code", "date"])
    for k in frames:
        assert frames_equal(frames[k]["frame"], before[k]) and len(frames[k]["frame"]) == 7


def test_combine_frames_duplicate_pk_raises_with_offending_request_ids():
    frames = frames_dict((RID_OLD1, "BRA"), (RID_OLD2, "CAN"))
    dup = pd.concat([frames[RID_OLD2]["frame"], frames[RID_OLD2]["frame"].iloc[[0]]], ignore_index=True)
    frames[RID_OLD2]["frame"] = dup
    with pytest.raises(StagingError) as ei:
        staging.combine_frames(frames, ["countryiso3code", "date"])
    assert RID_OLD2 in err_ids(ei.value) and RID_OLD1 not in err_ids(ei.value)


def test_combine_frames_pk_is_configurable():
    frames = frames_dict((RID_OLD1, "BRA"))
    f = frames[RID_OLD1]["frame"]
    dup = f.iloc[[0]].copy()
    dup["unit"] = "other"
    frames[RID_OLD1]["frame"] = pd.concat([f, dup], ignore_index=True)
    with pytest.raises(StagingError):
        staging.combine_frames(frames, ["countryiso3code", "date"])
    out = staging.combine_frames(frames, ["countryiso3code", "date", "unit"])
    assert len(out) == 8


def test_new_staging_merge_id_is_random_uuid4():
    a, b = staging.new_staging_merge_id(), staging.new_staging_merge_id()
    assert isinstance(a, str) and a != b
    assert str(uuid.UUID(a)) == a and uuid.UUID(a).version == 4


# ----------------------------------------------------------------------------- build_staging
def seed(tmp_path):
    land_pull(tmp_path, "BRA", LE, RID_OLD1)
    land_pull(tmp_path, "CAN", LE, RID_OLD2)


def test_build_staging_returns_and_writes_expected_artifacts(cfg, tmp_path):
    seed(tmp_path)
    df, frames, smid, path = staging.build_staging(cfg, LE)
    assert path == staging_path(tmp_path, LE)
    assert path == tmp_path / "storage" / "staging" / LE / f"{LE}_staging.psv"
    assert uuid.UUID(smid).version == 4
    assert set(frames) == {RID_OLD1, RID_OLD2}
    assert all(len(v["frame"]) == 7 for v in frames.values())  # per-country frames, before combining
    assert len(df) == 14

    lines = path.read_text().splitlines()
    assert lines[0] == "|".join(STAGING_COLS)
    assert len(lines) == 15
    assert all(line.count("|") == len(STAGING_COLS) - 1 for line in lines)
    back = psv_read(path)
    assert set(back["staging_merge_id"]) == {smid}
    assert set(back["request_id"]) == {RID_OLD1, RID_OLD2}
    keys = list(zip(back["countryiso3code"], back["date"]))
    assert keys == sorted(keys) and len(set(keys)) == 14


def test_build_staging_values_are_textual(cfg, tmp_path):
    land_pull(tmp_path, "BRA", LE, RID_OLD1, make_pull("BRA", LE, values={2024: 212812405, 2023: None,
                                                                        2022: 2185821610689.31}))
    land_pull(tmp_path, "CAN", LE, RID_OLD2)
    _, _, _, path = staging.build_staging(cfg, LE)
    back = psv_read(path)
    bra = dict(zip(back[back.countryiso3code == "BRA"]["date"], back[back.countryiso3code == "BRA"]["value"]))
    assert bra["2024"] == "212812405" and bra["2023"] == "" and bra["2022"] == "2185821610689.31"


def test_build_staging_new_id_each_run_and_file_overwritten(cfg, tmp_path):
    seed(tmp_path)
    _, _, id1, path = staging.build_staging(cfg, LE)
    _, _, id2, path2 = staging.build_staging(cfg, LE)
    assert id1 != id2 and path == path2
    assert set(psv_read(path)["staging_merge_id"]) == {id2}
    assert files_in(path.parent) == [path.name]


def test_build_staging_leaves_landing_untouched(cfg, tmp_path):
    seed(tmp_path)
    before = files_in(landing_dir(tmp_path, LE))
    staging.build_staging(cfg, LE)
    assert files_in(landing_dir(tmp_path, LE)) == before


def test_build_staging_duplicate_pk_raises(cfg, tmp_path):
    pull = make_pull("BRA", LE)
    pull[2][1][0] = copy.deepcopy(pull[0][1][0])  # same year twice in the same pull (bypassing ingest)
    land_pull(tmp_path, "BRA", LE, RID_OLD1, pull)
    with pytest.raises(StagingError):
        staging.build_staging(cfg, LE)


@pytest.mark.parametrize("bad", ["Bra|zil", "Bra\nzil", "Bra\rzil"])
def test_build_staging_pipe_or_newline_in_field_raises_staging_error(cfg, tmp_path, bad):
    pull = make_pull("BRA", LE)
    pull[0][1][0]["country"]["value"] = bad
    land_pull(tmp_path, "BRA", LE, RID_OLD1, pull)
    with pytest.raises(StagingError):
        staging.build_staging(cfg, LE)


def test_build_staging_incomplete_page_set_raises_staging_error(cfg, tmp_path):
    paths = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    paths[1].unlink()
    with pytest.raises(StagingError) as ei:
        staging.build_staging(cfg, LE)
    assert err_ids(ei.value) == {RID_OLD1}


def test_build_staging_custom_primary_key(cfg_dict, load_cfg, tmp_path):
    cfg_dict["indicators"][0]["primary_key"] = ["countryiso3code", "date", "unit"]
    cfg = load_cfg(cfg_dict)
    seed(tmp_path)
    df, _, _, path = staging.build_staging(cfg, LE)
    assert len(df) == 14 and len(psv_read(path)) == 14
