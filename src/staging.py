"""Staging: landing page files -> per-country frames -> combined staging table."""
import json
import logging
import uuid

import pandas as pd

from .config import get_indicator, resolve_path
from .errors import SchemaCheckError, StagingError
from .ingest import LANDING_RE, check_page_set, _scan_landing
from .io_utils import write_psv
from .schema_check import schema_check

log = logging.getLogger(__name__)

BIZ_COLS = ["countryiso3code", "date", "indicator_id", "indicator_name", "country_id",
            "country_name", "value", "unit", "obs_status", "decimal"]
FRAME_COLS = BIZ_COLS + ["request_id"]
STAGING_COLS = FRAME_COLS + ["staging_merge_id"]


def _s(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    return str(v)


def flatten_records(pages, request_id):
    rows = []
    for page in pages:
        for rec in page[1]:
            rows.append([_s(rec["countryiso3code"]), _s(rec["date"]), _s(rec["indicator"]["id"]),
                         _s(rec["indicator"]["value"]), _s(rec["country"]["id"]),
                         _s(rec["country"]["value"]), _s(rec["value"]), _s(rec["unit"]),
                         _s(rec["obs_status"]), _s(rec["decimal"]), request_id])
    return pd.DataFrame(rows, columns=FRAME_COLS, dtype=str)


def read_landing_file(path):
    m = LANDING_RE.match(path.name)
    if not m:
        raise StagingError(f"landing file name not recognised: {path.name}")
    c, ind, rid, page = m.group("C"), m.group("I"), m.group("R"), int(m.group("N"))

    def bad(msg):
        raise StagingError(f"{path.name}: {msg}", [rid])

    try:
        resp = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        bad(f"unreadable landing file ({e})")
    if not isinstance(resp, list) or len(resp) != 2:
        bad("landing file must hold a 2-element response array")
    if isinstance(resp[1], list):
        for rec in resp[1]:
            if not isinstance(rec, dict):
                continue
            if rec.get("countryiso3code") != c:
                bad(f"record countryiso3code {rec.get('countryiso3code')!r} does not match file name country {c}")
            i = rec.get("indicator")
            if isinstance(i, dict) and i.get("id") != ind:
                bad(f"record indicator.id {i.get('id')!r} does not match file name indicator {ind}")
    return c, ind, rid, page, resp


def build_country_frames(cfg, indicator_code):
    groups = _scan_landing(cfg, indicator_code)
    frames = {}
    for country, g in groups.items():
        if country not in cfg["countries"]:
            raise StagingError(f"landing files for country {country} which is not in the config",
                               list(g))
        if len(g) > 1:
            raise StagingError(f"more than one request_id for {country} {indicator_code}: {sorted(g)}",
                               list(g))
        for rid, paths in g.items():
            pages = {}
            for p in paths:
                _, _, _, n, resp = read_landing_file(p)
                pages[n] = resp
            check_page_set(pages, rid)
            for n in sorted(pages):
                try:
                    schema_check(pages[n], cfg, country, indicator_code, n)
                except SchemaCheckError as e:
                    raise StagingError(f"landing re-check failed for request_id {rid}: {e}", [rid]) from e
            frames[rid] = {"country": country,
                           "frame": flatten_records([pages[n] for n in sorted(pages)], rid)}
    return dict(sorted(frames.items()))


def combine_frames(frames, pk):
    if not frames:
        return pd.DataFrame(columns=FRAME_COLS, dtype=str)
    df = pd.concat([f["frame"] for f in frames.values()], ignore_index=True)
    dup = df.duplicated(subset=pk, keep=False)
    if dup.any():
        rids = sorted(set(df.loc[dup, "request_id"]))
        raise StagingError(f"duplicate primary key {pk} in staging: "
                           f"{df.loc[dup, pk].drop_duplicates().values.tolist()[:5]}", rids)
    return df.sort_values(pk, kind="stable").reset_index(drop=True)


def new_staging_merge_id():
    return str(uuid.uuid4())


def build_staging(cfg, indicator_code):
    pk = get_indicator(cfg, indicator_code)["primary_key"]
    frames = build_country_frames(cfg, indicator_code)
    df = combine_frames(frames, pk)
    smid = new_staging_merge_id()
    df = df.copy()
    df["staging_merge_id"] = smid
    df = df[STAGING_COLS]
    path = resolve_path(cfg, "staging", indicator_code) / f"{indicator_code}_staging.psv"
    try:
        write_psv(df, path)
    except ValueError as e:
        raise StagingError(f"cannot write staging file: {e}", list(frames)) from e
    return df, frames, smid, path
