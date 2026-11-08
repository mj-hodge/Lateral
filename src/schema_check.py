"""Schema check of a raw page response (array element 0: metadata, element 1: records)."""
import logging
import re

from .errors import SchemaCheckError

log = logging.getLogger(__name__)

META_INT_KEYS = ("page", "pages", "total")
META_STR_KEYS = ("sourceid", "lastupdated")
META_KEYS = ("page", "pages", "per_page", "total", "sourceid", "lastupdated")
REC_KEYS = ("indicator", "country", "countryiso3code", "date", "value", "unit", "obs_status", "decimal")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def collect_errors(page_response, cfg, country, indicator_code, expected_page):
    errs = []
    if not isinstance(page_response, list):
        return [f"response is not a list (got {type(page_response).__name__})"]
    if len(page_response) != 2:
        msg = f"response must be a list of length 2, got length {len(page_response)}"
        if len(page_response) == 1 and isinstance(page_response[0], dict) and "message" in page_response[0]:
            msg += f"; API error message: {page_response[0]['message']}"
        return [msg]
    meta, records = page_response
    if not isinstance(meta, dict):
        errs.append("metadata (element 0) is not a dict")
    else:
        for k in META_KEYS:
            if k not in meta:
                errs.append(f"metadata missing key {k}")
        for k in META_INT_KEYS:
            if k in meta and not _is_int(meta[k]):
                errs.append(f"metadata key {k} must be int, got {meta[k]!r}")
        if "per_page" in meta:
            pp = meta["per_page"]
            if not (_is_int(pp) or (isinstance(pp, str) and pp.isascii() and pp.isdigit())):
                errs.append(f"metadata key per_page must be int or digit string, got {pp!r}")
        for k in META_STR_KEYS:
            if k in meta and not isinstance(meta[k], str):
                errs.append(f"metadata key {k} must be str, got {meta[k]!r}")
        if _is_int(meta.get("page")) and meta["page"] != expected_page:
            errs.append(f"metadata page {meta['page']} != expected page {expected_page}")
        if _is_int(meta.get("pages")) and meta["pages"] < 1:
            errs.append(f"metadata pages must be >= 1, got {meta['pages']}")
        if _is_int(meta.get("total")) and meta["total"] < 1:
            errs.append(f"metadata total must be >= 1, got {meta['total']}")
        extra = set(meta) - set(META_KEYS)
        if extra:
            log.warning("unknown metadata keys tolerated: %s", sorted(extra))

    if not isinstance(records, list) or not records:
        errs.append("records (element 1) must be a non-empty list")
        return errs
    start, end = cfg["date_range"]["start"], cfg["date_range"]["end"]
    extra_rec = set()
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            errs.append(f"record {i}: not a dict")
            continue
        for k in REC_KEYS:
            if k not in rec:
                errs.append(f"record {i}: missing key {k}")
        extra_rec |= set(rec) - set(REC_KEYS)
        for obj in ("indicator", "country"):
            if obj in rec:
                o = rec[obj]
                if not isinstance(o, dict):
                    errs.append(f"record {i}: {obj} must be a dict")
                    continue
                for k in ("id", "value"):
                    if k not in o:
                        errs.append(f"record {i}: {obj} missing key {k}")
                    elif not isinstance(o[k], str):
                        errs.append(f"record {i}: {obj}.{k} must be str, got {o[k]!r}")
        if isinstance(rec.get("indicator"), dict) and isinstance(rec["indicator"].get("id"), str):
            if rec["indicator"]["id"] != indicator_code:
                errs.append(f"record {i}: indicator.id {rec['indicator']['id']!r} != {indicator_code!r}")
        if "countryiso3code" in rec:
            c = rec["countryiso3code"]
            if not isinstance(c, str):
                errs.append(f"record {i}: countryiso3code must be str, got {c!r}")
            elif c != country or not re.fullmatch(r"[A-Z]{3}", c):
                errs.append(f"record {i}: countryiso3code {c!r} != requested {country!r}")
        if "date" in rec:
            d = rec["date"]
            if not isinstance(d, str):
                errs.append(f"record {i}: date must be str, got {d!r}")
            elif not re.fullmatch(r"[0-9]{4}", d) or not (start <= int(d) <= end):
                errs.append(f"record {i}: date {d!r} is not a 4-digit year within {start}..{end}")
        if "value" in rec:
            v = rec["value"]
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                errs.append(f"record {i}: value must be number or null, got {v!r}")
        for k in ("unit", "obs_status"):
            if k in rec and not isinstance(rec[k], str):
                errs.append(f"record {i}: {k} must be str, got {rec[k]!r}")
        if "decimal" in rec and not _is_int(rec["decimal"]):
            errs.append(f"record {i}: decimal must be int, got {rec['decimal']!r}")
    if extra_rec:
        log.warning("unknown record keys tolerated: %s", sorted(extra_rec))
    return errs


def schema_check(page_response, cfg, country, indicator_code, expected_page):
    errs = collect_errors(page_response, cfg, country, indicator_code, expected_page)
    if errs:
        raise SchemaCheckError(
            f"schema check failed for country={country} indicator={indicator_code} "
            f"page={expected_page}: " + "; ".join(errs), errs)
