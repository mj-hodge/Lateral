"""Merge / upsert, lineage handling and idempotency (plan section 12 and the Merge row of section 15)."""
import pandas as pd
import pytest

from helpers import (BASE_COLS, BIZ_COLS, GDP, LE, META_COLS, STAGING_COLS, base_path, files_in, frames_equal,
                     psv_read, psv_write, stg_df, write_base)
from src import merge
from src.merge import MergeError

PK = ["countryiso3code", "date"]
R1, R2, R3 = "20261108T100000000001", "20261108T110000000002", "20261108T120000000003"
S1, S2, S3 = ("aaaaaaaa-0000-4000-8000-000000000001", "aaaaaaaa-0000-4000-8000-000000000002",
              "aaaaaaaa-0000-4000-8000-000000000003")


def S(rows, rid, smid):
    return stg_df(rows, rid, smid)


def row(c, d, v, **kw):
    return dict(countryiso3code=c, date=d, value=v, **kw)


def rows_by_key(df):
    return {(r.countryiso3code, r.date): r for r in df.itertuples(index=False)}


# ----------------------------------------------------------------------------- lineage helpers
def test_parse_lineage():
    assert merge.parse_lineage("") == []
    assert merge.parse_lineage("a") == ["a"]
    assert merge.parse_lineage("a;b;c") == ["a", "b", "c"]


def test_serialize_lineage():
    assert merge.serialize_lineage([]) == ""
    assert merge.serialize_lineage(["a"]) == "a"
    assert merge.serialize_lineage(["a", "b", "c"]) == "a;b;c"


def test_lineage_roundtrip():
    ids = [R1, R2, R3]
    assert merge.parse_lineage(merge.serialize_lineage(ids)) == ids


def test_append_lineage():
    assert merge.append_lineage("", R1) == R1
    assert merge.append_lineage(R1, R2) == f"{R1};{R2}"
    assert merge.append_lineage(f"{R1};{R2}", R3) == f"{R1};{R2};{R3}"


def test_append_lineage_never_appends_a_duplicate():
    assert merge.append_lineage(R1, R1) == R1
    assert merge.append_lineage(f"{R1};{R2}", R1) == f"{R1};{R2}"  # not moved to the end either
    assert merge.append_lineage(f"{R1};{R2}", R2) == f"{R1};{R2}"


@pytest.mark.parametrize("bad", ["a;b", "a|b"])
def test_append_lineage_rejects_ids_containing_separators(bad):
    with pytest.raises((AssertionError, ValueError, MergeError)):
        merge.append_lineage("", bad)


# ----------------------------------------------------------------------------- merge_frames
def test_first_run_creates_base_from_staging():
    s = S([row("CAN", 2018, "20"), row("BRA", 2019, "11"), row("BRA", 2018, "10")], R1, S1)
    out = merge.merge_frames(None, s, PK)
    assert list(out.columns) == BASE_COLS
    assert list(zip(out.countryiso3code, out.date)) == [("BRA", "2018"), ("BRA", "2019"), ("CAN", "2018")]
    assert out["request_id"].tolist() == [R1] * 3 and out["staging_merge_id"].tolist() == [S1] * 3
    assert out["request_id_linage"].tolist() == [R1] * 3
    assert out["staging_merged_id_linage"].tolist() == [S1] * 3
    assert out["value"].tolist() == ["10", "11", "20"]
    assert all(isinstance(v, str) for v in out.to_numpy().ravel())


def base_after_first():
    return merge.merge_frames(None, S([row("BRA", 2018, "10"), row("BRA", 2019, "11"), row("CAN", 2018, "20")], R1, S1), PK)


def test_new_key_insert_and_existing_key_update_and_base_only_retained():
    base = base_after_first()
    s2 = S([row("BRA", 2019, "99", unit="u"), row("BRA", 2020, "12"), row("BRA", 2018, "10")], R2, S2)
    out = merge.merge_frames(base, s2, PK)
    got = rows_by_key(out)
    assert set(got) == {("BRA", "2018"), ("BRA", "2019"), ("BRA", "2020"), ("CAN", "2018")}
    assert list(zip(out.countryiso3code, out.date)) == sorted(got)  # sorted by PK
    # updated key: new business values, scalars overwritten, lineage appended
    upd = got[("BRA", "2019")]
    assert upd.value == "99" and upd.unit == "u"
    assert upd.request_id == R2 and upd.staging_merge_id == S2
    assert upd.request_id_linage == f"{R1};{R2}" and upd.staging_merged_id_linage == f"{S1};{S2}"
    # unchanged existing key: values same, but still a touched row -> scalars + lineage advance
    same = got[("BRA", "2018")]
    assert same.value == "10" and same.request_id == R2 and same.request_id_linage == f"{R1};{R2}"
    # inserted key: staging ids, one-element lineage
    new = got[("BRA", "2020")]
    assert new.value == "12" and new.request_id == R2 and new.staging_merge_id == S2
    assert new.request_id_linage == R2 and new.staging_merged_id_linage == S2
    # base-only key: untouched (values, scalars, lineage)
    old = got[("CAN", "2018")]
    assert old.value == "20" and old.request_id == R1 and old.staging_merge_id == S1
    assert old.request_id_linage == R1 and old.staging_merged_id_linage == S1


def test_two_runs_identical_data_business_unchanged_lineage_grows_once():
    rows = [row("BRA", 2018, "10.5"), row("CAN", 2018, "20")]
    m1 = merge.merge_frames(None, S(rows, R1, S1), PK)
    m2 = merge.merge_frames(m1, S(rows, R2, S2), PK)
    assert frames_equal(m1[BIZ_COLS], m2[BIZ_COLS])
    assert m2["request_id"].tolist() == [R2, R2] and m2["staging_merge_id"].tolist() == [S2, S2]
    assert m2["request_id_linage"].tolist() == [f"{R1};{R2}"] * 2
    assert m2["staging_merged_id_linage"].tolist() == [f"{S1};{S2}"] * 2
    for r in m2.itertuples(index=False):
        assert r.request_id_linage.split(";")[-1] == r.request_id
        assert r.staging_merged_id_linage.split(";")[-1] == r.staging_merge_id


def test_three_runs_lineage_length_three_each_id_once():
    rows = [row("BRA", 2018, "10")]
    m = merge.merge_frames(None, S(rows, R1, S1), PK)
    m = merge.merge_frames(m, S(rows, R2, S2), PK)
    m = merge.merge_frames(m, S(rows, R3, S3), PK)
    assert m["request_id_linage"].iloc[0].split(";") == [R1, R2, R3]
    assert m["staging_merged_id_linage"].iloc[0].split(";") == [S1, S2, S3]


def test_replaying_the_same_staging_changes_nothing():
    rows = [row("BRA", 2018, "10"), row("CAN", 2018, "20")]
    s2 = S(rows, R2, S2)
    m1 = merge.merge_frames(None, S(rows, R1, S1), PK)
    m2 = merge.merge_frames(m1, s2, PK)
    m3 = merge.merge_frames(m2, s2, PK)
    assert frames_equal(m2, m3)
    assert m3["request_id_linage"].tolist() == [f"{R1};{R2}"] * 2  # no duplicate append


def test_values_stay_textual():
    s = S([row("BRA", 2018, "2185821610689.31"), row("BRA", 2019, ""), row("BRA", 2020, "007")], R1, S1)
    out = merge.merge_frames(None, s, PK)
    assert out["value"].tolist() == ["2185821610689.31", "", "007"]
    out2 = merge.merge_frames(out, S([row("BRA", 2018, "2185821610689.31")], R2, S2), PK)
    assert out2["value"].tolist() == ["2185821610689.31", "", "007"]


def test_update_can_blank_a_value():
    base = merge.merge_frames(None, S([row("BRA", 2018, "10")], R1, S1), PK)
    out = merge.merge_frames(base, S([row("BRA", 2018, "")], R2, S2), PK)
    assert out["value"].tolist() == [""]


def test_custom_primary_key_is_honored():
    pk = ["countryiso3code", "date", "unit"]
    base = merge.merge_frames(None, S([row("BRA", 2018, "1", unit="a"), row("BRA", 2018, "2", unit="b")], R1, S1), pk)
    assert len(base) == 2
    out = merge.merge_frames(base, S([row("BRA", 2018, "20", unit="b"), row("BRA", 2018, "3", unit="c")], R2, S2), pk)
    got = {r.unit: r for r in out.itertuples(index=False)}
    assert set(got) == {"a", "b", "c"}
    assert got["a"].value == "1" and got["a"].request_id == R1       # base-only
    assert got["b"].value == "20" and got["b"].request_id_linage == f"{R1};{R2}"  # updated
    assert got["c"].value == "3" and got["c"].request_id_linage == R2             # inserted


def test_base_with_duplicate_pk_raises_merge_error():
    base = base_after_first()
    base = pd.concat([base, base.iloc[[0]]], ignore_index=True)
    with pytest.raises(MergeError):
        merge.merge_frames(base, S([row("BRA", 2018, "10")], R2, S2), PK)


def test_column_mismatch_raises_merge_error():
    base = base_after_first()
    with pytest.raises(MergeError):
        merge.merge_frames(base.drop(columns=["unit"]), S([row("BRA", 2018, "10")], R2, S2), PK)
    s = S([row("BRA", 2018, "10")], R2, S2)
    with pytest.raises(MergeError):
        merge.merge_frames(base, s.drop(columns=["country_name"]), PK)
    s_extra = s.copy()
    s_extra["surprise"] = "x"
    with pytest.raises(MergeError):
        merge.merge_frames(base, s_extra, PK)


def test_no_field_contains_a_pipe_or_semicolon_except_lineage_separator():
    m = merge.merge_frames(None, S([row("BRA", 2018, "1")], R1, S1), PK)
    m = merge.merge_frames(m, S([row("BRA", 2018, "1")], R2, S2), PK)
    assert not any("|" in str(v) for v in m.to_numpy().ravel())
    assert ";" not in "".join(m[BIZ_COLS].astype(str).to_numpy().ravel())
    assert ";" not in m["request_id"].iloc[0] and ";" not in m["staging_merge_id"].iloc[0]


# ----------------------------------------------------------------------------- merge_indicator (files)
def write_staging(tmp_path, rows, rid, smid, ind=LE):
    from helpers import staging_path
    p = staging_path(tmp_path, ind)
    psv_write(p, stg_df(rows, rid, smid, ind))
    return p


def test_first_run_creates_base_file_with_14_columns(cfg, tmp_path):
    sp = write_staging(tmp_path, [row("BRA", 2019, "2185821610689.31"), row("BRA", 2018, "212812405")], R1, S1)
    out = merge.merge_indicator(cfg, LE, sp)
    assert out == base_path(tmp_path, LE) == tmp_path / "storage" / "data" / LE / f"{LE}.psv"
    lines = out.read_text().splitlines()
    assert lines[0] == "|".join(BASE_COLS) and len(lines) == 3
    assert all(l.count("|") == 13 for l in lines)
    assert "212812405" in lines[1] and "212812405.0" not in out.read_text()
    assert "2185821610689.31" in lines[2]
    assert files_in(out.parent) == [out.name]  # atomic write leaves no temp file
    base = psv_read(out)
    assert base["request_id"].tolist() == [R1] * 2 and base["staging_merge_id"].tolist() == [S1] * 2
    assert base["request_id_linage"].tolist() == [R1] * 2
    assert base["staging_merged_id_linage"].tolist() == [S1] * 2
    assert base["date"].tolist() == ["2018", "2019"]  # sorted by PK


def test_merge_indicator_update_insert_and_retain(cfg, tmp_path):
    merge.merge_indicator(cfg, LE, write_staging(
        tmp_path, [row("BRA", 2018, "10"), row("BRA", 2019, "11"), row("CAN", 2018, "20")], R1, S1))
    out = merge.merge_indicator(cfg, LE, write_staging(
        tmp_path, [row("BRA", 2019, "99"), row("BRA", 2020, "12")], R2, S2))
    got = rows_by_key(psv_read(out))
    assert got[("BRA", "2019")].value == "99" and got[("BRA", "2018")].value == "10"
    assert got[("BRA", "2020")].value == "12" and got[("CAN", "2018")].value == "20"
    assert got[("CAN", "2018")].request_id == R1 and got[("CAN", "2018")].request_id_linage == R1
    assert got[("BRA", "2018")].request_id_linage == R1  # not in the second staging: untouched
    assert got[("BRA", "2019")].request_id_linage == f"{R1};{R2}"
    assert got[("BRA", "2020")].request_id_linage == R2


def test_merge_indicator_identical_reruns(cfg, tmp_path):
    rows = [row("BRA", 2018, "10.5"), row("BRA", 2019, "2185821610689.31"), row("CAN", 2018, "20")]
    bp = merge.merge_indicator(cfg, LE, write_staging(tmp_path, rows, R1, S1))
    b1 = psv_read(bp)
    merge.merge_indicator(cfg, LE, write_staging(tmp_path, rows, R2, S2))
    b2 = psv_read(bp)
    assert frames_equal(b1[BIZ_COLS], b2[BIZ_COLS])
    merge.merge_indicator(cfg, LE, write_staging(tmp_path, rows, R3, S3))
    b3 = psv_read(bp)
    assert b2["request_id"].tolist() == [R2] * 3 and b2["staging_merge_id"].tolist() == [S2] * 3
    assert all(len(x.split(";")) == 2 for x in b2["request_id_linage"])
    assert all(len(x.split(";")) == 3 for x in b3["request_id_linage"])
    assert all(len(x.split(";")) == 3 for x in b3["staging_merged_id_linage"])
    assert b3["request_id_linage"].tolist() == [f"{R1};{R2};{R3}"] * 3
    assert b3["request_id"].tolist() == [R3] * 3
    assert frames_equal(b1[BIZ_COLS], b3[BIZ_COLS])


def test_merge_indicator_business_bytes_identical_across_reruns(cfg, tmp_path):
    rows = [row("BRA", 2018, "10.5"), row("CAN", 2018, "20")]
    bp = merge.merge_indicator(cfg, LE, write_staging(tmp_path, rows, R1, S1))
    first = [l.split("|")[:10] for l in bp.read_text().splitlines()]
    merge.merge_indicator(cfg, LE, write_staging(tmp_path, rows, R2, S2))
    second = [l.split("|")[:10] for l in bp.read_text().splitlines()]
    assert first == second


def test_replaying_the_same_staging_file_is_byte_identical(cfg, tmp_path):
    rows = [row("BRA", 2018, "10.5"), row("CAN", 2018, "20")]
    merge.merge_indicator(cfg, LE, write_staging(tmp_path, rows, R1, S1))
    sp = write_staging(tmp_path, rows, R2, S2)
    bp = merge.merge_indicator(cfg, LE, sp)
    once = bp.read_bytes()
    merge.merge_indicator(cfg, LE, sp)
    assert bp.read_bytes() == once


def test_base_is_readable_back_and_appendable(cfg, tmp_path):
    rows = [row("BRA", 2018, "1")]
    bp = merge.merge_indicator(cfg, LE, write_staging(tmp_path, rows, R1, S1))
    back = psv_read(bp)
    assert merge.parse_lineage(back["request_id_linage"].iloc[0]) == [R1]
    merge.merge_indicator(cfg, LE, write_staging(tmp_path, rows, R2, S2))
    back = psv_read(bp)
    assert merge.parse_lineage(back["request_id_linage"].iloc[0]) == [R1, R2]
    assert merge.parse_lineage(back["staging_merged_id_linage"].iloc[0]) == [S1, S2]


def test_merge_indicator_uses_per_indicator_primary_key(cfg_dict, load_cfg, tmp_path):
    cfg_dict["indicators"][0]["primary_key"] = ["countryiso3code", "date", "unit"]
    cfg = load_cfg(cfg_dict)
    merge.merge_indicator(cfg, LE, write_staging(tmp_path, [row("BRA", 2018, "1", unit="a"),
                                                              row("BRA", 2018, "2", unit="b")], R1, S1))
    out = merge.merge_indicator(cfg, LE, write_staging(tmp_path, [row("BRA", 2018, "5", unit="b")], R2, S2))
    got = {r.unit: r for r in psv_read(out).itertuples(index=False)}
    assert got["a"].value == "1" and got["b"].value == "5"
    assert len(got) == 2


def test_indicators_have_independent_base_tables(cfg, tmp_path):
    merge.merge_indicator(cfg, LE, write_staging(tmp_path, [row("BRA", 2018, "1")], R1, S1))
    merge.merge_indicator(cfg, GDP, write_staging(tmp_path, [row("BRA", 2018, "5000")], R2, S2, ind=GDP))
    assert psv_read(base_path(tmp_path, LE))["value"].tolist() == ["1"]
    assert psv_read(base_path(tmp_path, GDP))["value"].tolist() == ["5000"]


def test_merge_error_on_schema_drift_leaves_base_untouched(cfg, tmp_path):
    bp = merge.merge_indicator(cfg, LE, write_staging(tmp_path, [row("BRA", 2018, "1")], R1, S1))
    before = bp.read_bytes()
    bad = write_staging(tmp_path, [row("BRA", 2018, "2")], R2, S2)
    df = psv_read(bad).drop(columns=["country_name"])
    psv_write(bad, df)
    with pytest.raises(MergeError):
        merge.merge_indicator(cfg, LE, bad)
    assert bp.read_bytes() == before
    assert files_in(bp.parent) == [bp.name]


def test_merge_error_on_duplicate_pk_in_base_leaves_base_untouched(cfg, tmp_path):
    base = write_base(tmp_path, LE, [row("BRA", 2018, "1"), row("BRA", 2018, "2")], R1, S1)
    bp = base_path(tmp_path, LE)
    before = bp.read_bytes()
    with pytest.raises(MergeError):
        merge.merge_indicator(cfg, LE, write_staging(tmp_path, [row("BRA", 2019, "3")], R2, S2))
    assert bp.read_bytes() == before


def test_merge_error_is_a_pipeline_error():
    assert "PipelineError" in [c.__name__ for c in MergeError.__mro__]
