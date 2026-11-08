"""Archive step and leftover-staging crash recovery (plan sections 11 and the Archive row of section 15)."""
import pytest

from helpers import (BASE_COLS, LE, RID_OLD1, RID_OLD2, STAGING_COLS, archived_dir, base_path, files_in,
                     find_exc, land_pull, landing_dir, make_pull, page_name, psv_read, psv_write, stg_df,
                     staging_path, write_base, write_log, write_report)
from src import archive, merge

SMID = "11111111-1111-4111-8111-111111111111"
OTHER_SMID = "22222222-2222-4222-8222-222222222222"


# ----------------------------------------------------------------------------- archive_landing
def test_archive_moves_every_page_file_and_empties_landing(cfg, tmp_path):
    a = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    b = land_pull(tmp_path, "CAN", LE, RID_OLD2)
    contents = {p.name: p.read_text() for p in a + b}
    moved = archive.archive_landing(cfg, LE)
    assert sorted(p.name for p in moved) == sorted(contents)
    assert all(p.parent == archived_dir(tmp_path, LE) and p.exists() for p in moved)
    assert {p.name: p.read_text() for p in moved} == contents
    assert files_in(landing_dir(tmp_path, LE)) == []
    assert files_in(archived_dir(tmp_path, LE)) == sorted(contents)


def test_archive_only_touches_the_given_indicator(cfg, tmp_path):
    land_pull(tmp_path, "BRA", LE, RID_OLD1)
    other = land_pull(tmp_path, "BRA", "NY.GDP.PCAP.CD", RID_OLD2)
    archive.archive_landing(cfg, LE)
    assert all(p.exists() for p in other)


def test_archive_with_empty_landing_is_a_noop(cfg, tmp_path):
    assert archive.archive_landing(cfg, LE) == []
    assert files_in(landing_dir(tmp_path, LE)) == []


def test_archive_existing_target_raises_collision_and_moves_nothing(cfg, tmp_path):
    paths = land_pull(tmp_path, "BRA", LE, RID_OLD1)
    clash = archived_dir(tmp_path, LE) / paths[2].name
    clash.parent.mkdir(parents=True)
    clash.write_text("PRE-EXISTING")
    with pytest.raises(find_exc("FileCollisionError")):
        archive.archive_landing(cfg, LE)
    assert files_in(landing_dir(tmp_path, LE)) == sorted(p.name for p in paths)
    assert clash.read_text() == "PRE-EXISTING"
    assert files_in(archived_dir(tmp_path, LE)) == [clash.name]


# ----------------------------------------------------------------------------- merge_leftover_staging
def leftover(tmp_path, *, report="pass", archived_pages=(1, 2, 3), with_base=False, smid=SMID,
             landing_files=False, log=True):
    """Crash window between archive and merge: staging present, landing empty."""
    rows = [{"countryiso3code": "BRA", "date": y, "value": 10 + y - 2018} for y in range(2018, 2025)]
    psv_write(staging_path(tmp_path, LE), stg_df(rows, RID_OLD1, smid))
    if report == "pass":
        write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 7, "PASS")], smid=smid)
    elif report == "fail":
        write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 6, "FAIL")], smid=smid)
    elif report == "mixed":
        write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 7, "PASS"), (RID_OLD2, "CAN", 7, 6, "FAIL")], smid=smid)
    pull = make_pull("BRA", LE)
    d = archived_dir(tmp_path, LE)
    d.mkdir(parents=True, exist_ok=True)
    import json
    for n in archived_pages:
        (d / page_name("BRA", LE, RID_OLD1, n)).write_text(json.dumps(pull[n - 1]))
    if log:
        write_log(tmp_path, RID_OLD1, 7, 3, "BRA", LE)
    if landing_files:
        land_pull(tmp_path, "CAN", LE, RID_OLD2)
    if with_base:
        write_base(tmp_path, LE, [{"countryiso3code": "CAN", "date": 2018, "value": 99}],
                   "20251231T000000000000", OTHER_SMID)


def snapshot(path):
    return path.read_bytes() if path.exists() else None


@pytest.mark.parametrize("with_base", [False, True])
def test_leftover_staging_is_merged_once(cfg, tmp_path, with_base):
    leftover(tmp_path, with_base=with_base)
    result = merge.merge_leftover_staging(cfg, LE)
    bp = base_path(tmp_path, LE)
    assert result == bp and bp.exists()
    base = psv_read(bp)
    assert list(base.columns) == BASE_COLS
    bra = base[base.countryiso3code == "BRA"]
    assert len(bra) == 7
    assert set(bra["staging_merge_id"]) == {SMID} and set(bra["request_id"]) == {RID_OLD1}
    assert set(bra["staging_merged_id_linage"]) == {SMID}
    if with_base:
        assert len(base[base.countryiso3code == "CAN"]) == 1  # existing row retained
    first = bp.read_bytes()
    assert merge.merge_leftover_staging(cfg, LE) is None  # id already in lineage -> no-op
    assert bp.read_bytes() == first


@pytest.mark.parametrize("with_base", [False, True])
@pytest.mark.parametrize("scenario", ["fail_report", "no_report", "mixed_report", "missing_archived_page",
                                      "no_archived_pages", "landing_not_empty"])
def test_leftover_staging_not_merged_when_guards_fail(cfg, tmp_path, with_base, scenario):
    kwargs = {
        "fail_report": dict(report="fail"),
        "no_report": dict(report=None),
        "mixed_report": dict(report="mixed"),
        "missing_archived_page": dict(archived_pages=(1, 3)),
        "no_archived_pages": dict(archived_pages=()),
        "landing_not_empty": dict(landing_files=True),
    }[scenario]
    leftover(tmp_path, with_base=with_base, **kwargs)
    bp = base_path(tmp_path, LE)
    before = snapshot(bp)
    stg_before = staging_path(tmp_path, LE).read_bytes()
    assert merge.merge_leftover_staging(cfg, LE) is None
    assert snapshot(bp) == before  # base absent stays absent, base present stays byte-identical
    assert staging_path(tmp_path, LE).read_bytes() == stg_before  # staging is left alone


def test_leftover_report_for_a_different_staging_id_does_not_count(cfg, tmp_path):
    leftover(tmp_path, report=None)
    write_report(tmp_path, LE, [(RID_OLD1, "BRA", 7, 7, "PASS")], smid=OTHER_SMID)
    assert merge.merge_leftover_staging(cfg, LE) is None
    assert not base_path(tmp_path, LE).exists()


def test_leftover_not_merged_when_staging_missing(cfg, tmp_path):
    assert merge.merge_leftover_staging(cfg, LE) is None
    assert not base_path(tmp_path, LE).exists()


def test_leftover_not_merged_twice_even_if_id_is_in_lineage(cfg, tmp_path):
    leftover(tmp_path)
    merge.merge_leftover_staging(cfg, LE)
    bp = base_path(tmp_path, LE)
    snap = bp.read_bytes()
    assert merge.merge_leftover_staging(cfg, LE) is None and bp.read_bytes() == snap
    assert psv_read(bp)["staging_merged_id_linage"].tolist() == [SMID] * 7
