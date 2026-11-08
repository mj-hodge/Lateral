"""Merge (upsert) of the staging table into the per-indicator base table, with lineage columns."""
import logging

import pandas as pd

from .config import get_indicator, resolve_path
from .errors import MergeError
from .io_utils import read_psv, write_psv

log = logging.getLogger(__name__)

BIZ_COLS = ["countryiso3code", "date", "indicator_id", "indicator_name", "country_id",
            "country_name", "value", "unit", "obs_status", "decimal"]
STAGING_COLS = BIZ_COLS + ["request_id", "staging_merge_id"]
BASE_COLS = STAGING_COLS + ["request_id_linage", "staging_merged_id_linage"]


def parse_lineage(s):
    if s is None or s == "":
        return []
    return s.split(";")


def serialize_lineage(ids):
    return ";".join(ids)


def append_lineage(existing, new_id):
    if ";" in new_id or "|" in new_id or new_id == "":
        raise MergeError(f"lineage id must be non-empty and contain neither ';' nor '|': {new_id!r}")
    ids = parse_lineage(existing)
    if new_id not in ids:
        ids.append(new_id)
    return serialize_lineage(ids)


def _check_unique(df, pk, what):
    if df.duplicated(subset=pk).any():
        raise MergeError(f"duplicate primary key {pk} in {what}")


def merge_frames(base_df, staging_df, pk):
    if list(staging_df.columns) != STAGING_COLS:
        raise MergeError(f"staging columns {list(staging_df.columns)} != expected {STAGING_COLS}")
    _check_unique(staging_df, pk, "staging")
    staging_df = staging_df.astype(object)
    rows = {}
    if base_df is not None:
        if list(base_df.columns) != BASE_COLS:
            raise MergeError(f"base columns {list(base_df.columns)} != expected {BASE_COLS}")
        _check_unique(base_df, pk, "base")
        for rec in base_df.astype(object).to_dict("records"):
            rows[tuple(rec[c] for c in pk)] = rec
    for rec in staging_df.to_dict("records"):
        key = tuple(rec[c] for c in pk)
        old = rows.get(key)
        new = dict(rec)
        if old is None:
            new["request_id_linage"] = append_lineage("", rec["request_id"])
            new["staging_merged_id_linage"] = append_lineage("", rec["staging_merge_id"])
        else:
            new["request_id_linage"] = append_lineage(old["request_id_linage"], rec["request_id"])
            new["staging_merged_id_linage"] = append_lineage(old["staging_merged_id_linage"],
                                                             rec["staging_merge_id"])
        rows[key] = new
    out = pd.DataFrame(list(rows.values()), columns=BASE_COLS, dtype=object)
    out = out.sort_values(pk, kind="stable").reset_index(drop=True)
    return out.astype(str) if len(out) else out


def _base_path(cfg, indicator_code):
    return resolve_path(cfg, "data", indicator_code) / f"{indicator_code}.psv"


def merge_indicator(cfg, indicator_code, staging_path):
    pk = get_indicator(cfg, indicator_code)["primary_key"]
    staging_df = read_psv(staging_path)
    bp = _base_path(cfg, indicator_code)
    base_df = read_psv(bp) if bp.exists() else None
    merged = merge_frames(base_df, staging_df, pk)
    try:
        write_psv(merged, bp)
    except ValueError as e:
        raise MergeError(f"cannot write base table: {e}") from e
    return bp


def merge_leftover_staging(cfg, indicator_code):
    from .ingest import _scan_landing  # local import: avoid cycles
    if any(g for g in _scan_landing(cfg, indicator_code).values()):
        return None
    landing = resolve_path(cfg, "landing", indicator_code)
    if landing.is_dir() and any(p.suffix == ".json" for p in landing.iterdir()):
        return None
    sp = resolve_path(cfg, "staging", indicator_code) / f"{indicator_code}_staging.psv"
    if not sp.exists():
        return None
    try:
        st = read_psv(sp)
    except Exception:
        return None
    if list(st.columns) != STAGING_COLS or st.empty:
        return None
    ids = set(st["staging_merge_id"])
    if len(ids) != 1:
        return None
    smid = ids.pop()
    bp = _base_path(cfg, indicator_code)
    if bp.exists():
        base = read_psv(bp)
        if "staging_merged_id_linage" not in base.columns:
            return None
        if any(smid in parse_lineage(x) for x in base["staging_merged_id_linage"]):
            return None
    reports = resolve_path(cfg, "row_count_reports", indicator_code)
    matches = sorted(reports.glob(f"row_count_*_{smid}.psv")) if reports.is_dir() else []
    if not matches:
        return None
    rep = read_psv(matches[-1])
    if rep.empty or "status" not in rep.columns or not (rep["status"] == "PASS").all():
        return None
    from .dq_tests import read_request_log
    from .ingest import landing_filename
    archived = resolve_path(cfg, "archived", indicator_code)
    for rid, grp in st.groupby("request_id"):
        try:
            pages = read_request_log(cfg, indicator_code, rid)["pages"]
        except (OSError, ValueError):
            return None
        country = grp["countryiso3code"].iloc[0]
        for n in range(1, pages + 1):
            if not (archived / landing_filename(country, indicator_code, rid, n)).exists():
                return None
    log.info("merging leftover staging %s for %s", smid, indicator_code)
    return merge_indicator(cfg, indicator_code, sp)
