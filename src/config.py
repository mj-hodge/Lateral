"""Config loading and validation."""
import re
from datetime import datetime
from pathlib import Path

import yaml

from .errors import ConfigError

BUSINESS_COLUMNS = ["countryiso3code", "date", "indicator_id", "indicator_name", "country_id",
                    "country_name", "value", "unit", "obs_status", "decimal"]
RID_FORMAT_RE = re.compile(r"^%Y%m%d[^%]*%H[^%]*%M[^%]*%S[^%]*%f$")
REQUIRED_PATHS = ["landing", "staging", "archived", "rejected", "staging_failed_marker", "data",
                  "request_log", "row_count_reports", "run_log", "analysis_out", "analysis_sql"]


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _need(d, key, where):
    if not isinstance(d, dict) or key not in d:
        raise ConfigError(f"missing config key {where}{key}")
    return d[key]


def _sample_ids(fmt):
    a = datetime(2001, 2, 3, 4, 5, 6, 7)
    b = datetime(2031, 12, 30, 23, 59, 58, 999999)
    return a.strftime(fmt), b.strftime(fmt)


def validate_config(cfg):
    if not isinstance(cfg, dict):
        raise ConfigError("config must be a mapping")
    api = _need(cfg, "api", "")
    for k in ("base_url", "path_template"):
        _need(api, k, "api.")
    tpl = api["path_template"]
    if not isinstance(tpl, str) or "{country}" not in tpl or "{indicator}" not in tpl:
        raise ConfigError("api.path_template must contain {country} and {indicator}")
    if api.get("format", "json") != "json":
        raise ConfigError(f"api.format must be 'json', got {api.get('format')!r}")
    api.setdefault("format", "json")
    ts = api.setdefault("timeout_seconds", 30)
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        raise ConfigError(f"api.timeout_seconds must be a positive number, got {ts!r}")
    if "per_page" in api:
        pp = api["per_page"]
        if pp is None:
            pass
        elif _is_int(pp) and pp > 0:
            pass
        elif isinstance(pp, str) and pp.isdigit() and int(pp) > 0:
            api["per_page"] = int(pp)
        else:
            raise ConfigError(f"api.per_page must be null, a positive int or a digit string: {pp!r}")
    else:
        api["per_page"] = None

    dr = _need(cfg, "date_range", "")
    start, end = _need(dr, "start", "date_range."), _need(dr, "end", "date_range.")
    if not (_is_int(start) and _is_int(end)) or start > end:
        raise ConfigError("date_range start/end must be ints with start <= end")

    countries = _need(cfg, "countries", "")
    if not isinstance(countries, list) or not countries:
        raise ConfigError("countries must be a non-empty list")
    if len(set(countries)) != len(countries):
        raise ConfigError("duplicate countries")
    for c in countries:
        if not isinstance(c, str) or not re.fullmatch(r"[A-Z]{3}", c):
            raise ConfigError(f"country must be an ISO3 code (3 capital letters): {c!r}")

    inds = _need(cfg, "indicators", "")
    if not isinstance(inds, list) or not inds:
        raise ConfigError("indicators must be a non-empty list")
    codes = []
    for ind in inds:
        for k in ("code", "name", "table_name", "primary_key"):
            _need(ind, k, "indicators[].")
        code = ind["code"]
        if not isinstance(code, str) or not code or "/" in code or "|" in code:
            raise ConfigError(f"invalid indicator code {code!r}")
        codes.append(code)
        pk = ind["primary_key"]
        if not isinstance(pk, list) or not pk:
            raise ConfigError(f"primary_key of {code} must be a non-empty list")
        bad = [c for c in pk if c not in BUSINESS_COLUMNS]
        if bad:
            raise ConfigError(f"primary_key of {code} has non-business columns: {bad}")
        if "countryiso3code" not in pk:
            raise ConfigError(f"primary_key of {code} must include countryiso3code")
    if len(set(codes)) != len(codes):
        raise ConfigError("duplicate indicator codes")
    tnames = [i["table_name"] for i in inds]
    if len(set(tnames)) != len(tnames):
        raise ConfigError("duplicate table_name")

    retry = _need(cfg, "retry", "")
    for k in ("sleep_seconds", "max_retries"):
        v = _need(retry, k, "retry.")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"retry.{k} must be >= 0")
    if not _is_int(retry["max_retries"]):
        raise ConfigError("retry.max_retries must be an int")
    retry.setdefault("status_codes", [429])
    retry.setdefault("retry_on_5xx", True)
    retry.setdefault("retry_on_connection_errors", True)
    for k in ("retry_on_5xx", "retry_on_connection_errors"):
        if not isinstance(retry[k], bool):
            raise ConfigError(f"retry.{k} must be a boolean")
    if not isinstance(retry["status_codes"], list):
        raise ConfigError("retry.status_codes must be a list")

    fmt = _need(_need(cfg, "request_id", ""), "format", "request_id.")
    if not isinstance(fmt, str) or "%f" not in fmt or not RID_FORMAT_RE.match(fmt):
        raise ConfigError(f"request_id.format must match %Y%m%d..%H..%M..%S..%f: {fmt!r}")
    a, b = _sample_ids(fmt)
    if len(a) != len(b) or not a < b:
        raise ConfigError("request_id.format must be fixed-width and sort chronologically")
    for s in (a, b):
        if re.search(r"[;|/_\s]", s):
            raise ConfigError("request_id.format renders characters from ; | / _ or whitespace")

    paths = _need(cfg, "paths", "")
    for k in REQUIRED_PATHS:
        _need(paths, k, "paths.")

    analysis = _need(cfg, "analysis", "")
    of = _need(analysis, "output_file", "analysis.")
    if (not isinstance(of, str) or not of or "/" in of or "\\" in of or of in (".", "..")
            or ".." in of):
        raise ConfigError(f"analysis.output_file must be a bare file name: {of!r}")
    tables = _need(analysis, "tables", "analysis.")
    if not isinstance(tables, dict):
        raise ConfigError("analysis.tables must be a mapping")
    for alias, code in tables.items():
        if code not in codes:
            raise ConfigError(f"analysis.tables[{alias}] refers to unconfigured indicator {code!r}")


def _resolve(root, template, indicator=None):
    t = template.replace("{indicator}", indicator) if indicator is not None else template
    p = Path(t)
    full = p if p.is_absolute() else root / p
    import os
    full = Path(os.path.normpath(full))
    try:
        full.relative_to(root)
    except ValueError:
        raise ConfigError(f"path escapes project root: {template}")
    return full


def load_config(path):
    path = Path(path).resolve()
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    validate_config(cfg)
    cfg["_root"] = path.parent.parent
    for key, tpl in cfg["paths"].items():
        if not isinstance(tpl, str):
            raise ConfigError(f"paths.{key} must be a string")
        _resolve(cfg["_root"], tpl, "x")
    return cfg


def resolve_path(cfg, key, indicator=None):
    if key not in cfg["paths"]:
        raise ConfigError(f"unknown path key {key}")
    return _resolve(cfg["_root"], cfg["paths"][key], indicator)


def get_indicator(cfg, code):
    for ind in cfg["indicators"]:
        if ind["code"] == code:
            return ind
    raise ConfigError(f"indicator not configured: {code}")
