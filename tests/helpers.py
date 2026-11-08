"""Shared builders for the test-suite (plain functions/classes, no pipeline code).

Everything here is derived from the plan (sections 3, 5, 15) and the prompt's API example.
conftest.py exposes these as fixtures; test modules may also import them directly
(`from helpers import ...`), because tests/ is on sys.path under pytest's default import mode.
"""
import copy
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
import yaml

LE = "SP.DYN.LE00.IN"
GDP = "NY.GDP.PCAP.CD"
POP = "SP.POP.TOTL"
YEARS = list(range(2018, 2025))

ISO2 = {"BRA": "BR", "CAN": "CA", "DEU": "DE", "GBR": "GB", "IND": "IN",
        "JPN": "JP", "MEX": "MX", "USA": "US", "ZAF": "ZA"}
COUNTRY_NAMES = {"BRA": "Brazil", "CAN": "Canada", "DEU": "Germany", "GBR": "United Kingdom",
                 "IND": "India", "JPN": "Japan", "MEX": "Mexico", "USA": "United States",
                 "ZAF": "South Africa"}
INDICATOR_NAMES = {LE: "Life expectancy at birth, total (years)",
                   GDP: "GDP per capita (current US$)",
                   POP: "Population, total"}

BIZ_COLS = ["countryiso3code", "date", "indicator_id", "indicator_name", "country_id",
            "country_name", "value", "unit", "obs_status", "decimal"]
STAGING_COLS = BIZ_COLS + ["request_id", "staging_merge_id"]
BASE_COLS = BIZ_COLS + ["request_id", "staging_merge_id", "request_id_linage",
                        "staging_merged_id_linage"]
META_COLS = ["request_id", "staging_merge_id", "request_id_linage", "staging_merged_id_linage"]
FRAME_COLS = BIZ_COLS + ["request_id"]  # flatten_records output
REPORT_COLS = ["request_id", "country_code", "indicator_code", "expected_total", "actual_rows",
               "status", "detail", "staging_merge_id", "checked_at"]

START_NOW = datetime(2026, 11, 8, 15, 30, 45, 123456, tzinfo=timezone.utc)
RID_FMT = "%Y%m%dT%H%M%S%f"
RID_OLD1 = "20260101T000000000001"   # sorts before every id fake_now produces
RID_OLD2 = "20260101T000000000002"
RID_OLD3 = "20260101T000000000003"


def rid_at(n):
    """The request_id fake_now returns on its n-th call (n starts at 0)."""
    return (START_NOW + timedelta(microseconds=n)).strftime(RID_FMT)


# --------------------------------------------------------------------------- payload builders
def default_value(country, indicator, year):
    base = sum(ord(c) for c in country)
    if indicator == LE:
        return round(60 + (base % 20) + (year - 2018) * 0.1, 1)
    if indicator == GDP:
        return round(5000 + base * 10 + (year - 2018) * 123.45, 2)
    return 1_000_000 * (base % 7 + 1) + (year - 2018) * 1000 + base


def make_record(country, indicator, year, value):
    return {
        "indicator": {"id": indicator, "value": INDICATOR_NAMES.get(indicator, indicator)},
        "country": {"id": ISO2.get(country, country[:2]),
                    "value": COUNTRY_NAMES.get(country, country)},
        "countryiso3code": country,
        "date": str(year),
        "value": value,
        "unit": "",
        "obs_status": "",
        "decimal": 0,
    }


def make_page(country, indicator, years, page, pages, per_page, total, values=None):
    meta = {"page": page, "pages": pages, "per_page": per_page, "total": total,
            "sourceid": "2", "lastupdated": "2026-10-08"}
    values = values or {}
    records = [make_record(country, indicator, y,
                           values[y] if y in values else default_value(country, indicator, y))
               for y in years]
    return [meta, records]


def make_pull(country, indicator, years=YEARS, per_page=3, values=None, newest_first=True):
    """All pages of one pull, in the shape of the live API (3+3+1 records for 7 years at 3/page)."""
    ys = sorted(years, reverse=newest_first)
    chunks = [ys[i:i + per_page] for i in range(0, len(ys), per_page)] or [[]]
    pages = len(chunks)
    return [make_page(country, indicator, chunk, n, pages, per_page, len(ys), values)
            for n, chunk in enumerate(chunks, 1)]


def drop_last_record(pull):
    """Same pull with one record missing (metadata total unchanged), so the records no longer add up.

    The record is removed from page 2 (3 -> 2 records) rather than from the 1-record last page, because
    an empty page fails the schema check (non-empty records list) before row_count could ever see it.
    With a single-page pull the last record of page 1 is removed instead."""
    pull = copy.deepcopy(pull)
    target = pull[1] if len(pull) > 1 else pull[0]
    target[1].pop()
    return pull


# --------------------------------------------------------------------------- fakes
class FakeResponse:
    NOT_JSON = object()

    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self.payload = payload
        self.text = text if text is not None else (
            "" if payload is FakeResponse.NOT_JSON else json.dumps(payload))

    def json(self):
        if self.payload is FakeResponse.NOT_JSON:
            raise requests.exceptions.JSONDecodeError("not json", self.text, 0)
        return self.payload


_URL_RE = re.compile(r"/country/(?P<c>[^/]+)/indicator/(?P<i>[^/?]+)$")


class FakeHttpGet:
    """Callable (url, timeout=None). Records every call; scripted per (country, indicator, page).

    responses: {(country, indicator, page): item | tuple_of_items}; a tuple is a sequence consumed
    call by call (exhausted -> AssertionError). An item is a FakeResponse, an Exception instance
    (raised), or a raw payload (served as HTTP 200).
    pulls: {(country, indicator): [page_response, ...]} overrides for the automatic fallback.
    Anything unscripted is answered from make_pull(country, indicator) unless auto=False.
    """

    def __init__(self, responses=None, pulls=None, auto=True, per_page=3):
        self.responses = dict(responses or {})
        self.pulls = dict(pulls or {})
        self.auto = auto
        self.per_page = per_page
        self.calls = []      # urls
        self.keys = []       # (country, indicator, page)
        self.timeouts = []

    def __call__(self, url, timeout=None):
        parsed = urlparse(url)
        m = _URL_RE.search(parsed.path)
        assert m, f"unexpected URL path in fake http_get: {url}"
        q = parse_qs(parsed.query)
        page = int(q.get("page", ["1"])[0])
        key = (m.group("c"), m.group("i"), page)
        self.calls.append(url)
        self.keys.append(key)
        self.timeouts.append(timeout)
        if key in self.responses:
            item = self.responses[key]
            if isinstance(item, tuple):
                assert item, f"scripted sequence exhausted for {key}"
                head, rest = item[0], item[1:]
                self.responses[key] = rest
                item = head
            return self._serve(item)
        pull = self.pulls.get(key[:2])
        if pull is None:
            assert self.auto, f"unscripted request {key}"
            pull = make_pull(key[0], key[1], per_page=self.per_page)
        assert 1 <= page <= len(pull), f"page {page} outside the {len(pull)} pages of {key[:2]}"
        return FakeResponse(200, pull[page - 1])

    @staticmethod
    def _serve(item):
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(200, item)

    def calls_for(self, country=None, indicator=None):
        return [k for k in self.keys
                if (country is None or k[0] == country) and (indicator is None or k[1] == indicator)]


class FakeSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


class FakeNow:
    """Zero-arg callable returning successive aware UTC datetimes (+1 microsecond per call)."""

    def __init__(self, start=START_NOW):
        self.start = start
        self.count = 0

    def __call__(self):
        value = self.start + timedelta(microseconds=self.count)
        self.count += 1
        return value


# --------------------------------------------------------------------------- config
def base_cfg_dict():
    return {
        "api": {"base_url": "https://api.worldbank.org/v2",
                "path_template": "country/{country}/indicator/{indicator}",
                "format": "json", "per_page": 3, "timeout_seconds": 17},
        "date_range": {"start": 2018, "end": 2024},
        "countries": ["BRA", "CAN"],
        "indicators": [
            {"code": LE, "name": "Life expectancy at birth, total years",
             "table_name": "life_expectancy_years", "primary_key": ["countryiso3code", "date"]},
            {"code": GDP, "name": "GDP per capita, current US dollars",
             "table_name": "gdp_per_capita_current_usd", "primary_key": ["countryiso3code", "date"]},
        ],
        "retry": {"status_codes": [429], "retry_on_5xx": True, "retry_on_connection_errors": True,
                  "sleep_seconds": 60, "max_retries": 3},
        "request_id": {"format": "%Y%m%dT%H%M%S%f"},
        "paths": {
            "landing": "storage/landing/{indicator}",
            "staging": "storage/staging/{indicator}",
            "archived": "storage/archived/{indicator}",
            "rejected": "storage/rejected/{indicator}",
            "staging_failed_marker": "storage/staging/{indicator}/staging_failed_request_ids.txt",
            "data": "storage/data/{indicator}",
            "request_log": "log/{indicator}",
            "row_count_reports": "tests/{indicator}/row_count",
            "run_log": "log/pipeline.log",
            "analysis_out": "sql/analysis",
            "analysis_sql": "sql/code/analysis.sql",
        },
        "analysis": {"output_file": "life_expectancy_by_gdp_per_capita.psv",
                     "tables": {"life_expectancy": LE, "gdp_per_capita": GDP}},
    }


def write_cfg(root, cfg_dict, rel="config/pipeline.yaml"):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg_dict, sort_keys=False))
    return path


# --------------------------------------------------------------------------- on-disk layout (plan section 4)
def landing_dir(root, ind):
    return Path(root) / "storage" / "landing" / ind


def archived_dir(root, ind):
    return Path(root) / "storage" / "archived" / ind


def rejected_dir(root, ind):
    return Path(root) / "storage" / "rejected" / ind


def staging_path(root, ind):
    return Path(root) / "storage" / "staging" / ind / f"{ind}_staging.psv"


def marker_path(root, ind):
    return Path(root) / "storage" / "staging" / ind / "staging_failed_request_ids.txt"


def base_path(root, ind):
    return Path(root) / "storage" / "data" / ind / f"{ind}.psv"


def log_path(root, ind, rid):
    return Path(root) / "log" / ind / f"{rid}.log"


def reports_dir(root, ind):
    return Path(root) / "tests" / ind / "row_count"


def analysis_out(root, name="life_expectancy_by_gdp_per_capita.psv"):
    return Path(root) / "sql" / "analysis" / name


def files_in(directory):
    d = Path(directory)
    return sorted(p.name for p in d.iterdir() if p.is_file()) if d.exists() else []


def page_name(country, ind, rid, n):
    return f"{country}_{ind}_{rid}_p{n}.json"


def write_log(root, rid, total, pages, country, ind):
    p = log_path(root, ind, rid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{rid}|{total}|{pages}|{country}|{ind}\n")
    return p


def land_pull(root, country, ind, rid, pull=None, dest="landing", with_log=True):
    """Write one pull's page files (and its log) directly, independent of the code under test."""
    pull = pull if pull is not None else make_pull(country, ind)
    d = Path(root) / "storage" / dest / ind
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for n, page in enumerate(pull, 1):
        p = d / page_name(country, ind, rid, n)
        p.write_text(json.dumps(page))
        paths.append(p)
    if with_log:
        write_log(root, rid, pull[0][0]["total"], pull[0][0]["pages"], country, ind)
    return paths


def write_report(root, ind, rows, smid="0f0f0f0f-0000-4000-8000-000000000001",
                 ts="20261108T160000000000"):
    """rows: [(request_id, country, expected, actual, status)] -> reports_dir/row_count_<ts>_<smid>.psv"""
    d = reports_dir(root, ind)
    d.mkdir(parents=True, exist_ok=True)
    lines = ["|".join(REPORT_COLS)]
    for rid, c, exp, act, status in rows:
        lines.append("|".join([rid, c, ind, str(exp), str(act), status, "", smid,
                               "2026-11-08T16:00:00Z"]))
    p = d / f"row_count_{ts}_{smid}.psv"
    p.write_text("\n".join(lines) + "\n")
    return p


# --------------------------------------------------------------------------- psv / frame helpers
def psv_write(path, df):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, sep="|", index=False, lineterminator="\n")


def psv_read(path):
    return pd.read_csv(path, sep="|", dtype=str, keep_default_na=False)


def stg_df(rows, rid, smid, ind=LE, cols=STAGING_COLS):
    """Build an all-str staging frame. rows: dicts with countryiso3code, date, value (+ overrides)."""
    out = []
    for r in rows:
        rec = {"countryiso3code": r["countryiso3code"], "date": str(r["date"]),
               "indicator_id": ind, "indicator_name": INDICATOR_NAMES.get(ind, ind),
               "country_id": ISO2.get(r["countryiso3code"], "XX"),
               "country_name": COUNTRY_NAMES.get(r["countryiso3code"], "X"),
               "value": str(r["value"]), "unit": "", "obs_status": "", "decimal": "0",
               "request_id": rid, "staging_merge_id": smid}
        rec.update({k: str(v) for k, v in r.items() if k in rec})
        out.append(rec)
    return pd.DataFrame(out, columns=cols, dtype=str)


def frame_for(country, ind, rid, pull=None):
    """The per-country frame (FRAME_COLS, all str) a correct build_country_frames would produce."""
    pull = pull if pull is not None else make_pull(country, ind)
    rows = []
    for page in pull:
        for rec in page[1]:
            v = rec["value"]
            sv = "" if v is None else (repr(v) if isinstance(v, float) else str(v))
            rows.append([rec["countryiso3code"], rec["date"], rec["indicator"]["id"],
                         rec["indicator"]["value"], rec["country"]["id"], rec["country"]["value"],
                         sv, rec["unit"], rec["obs_status"], str(rec["decimal"]), rid])
    return pd.DataFrame(rows, columns=FRAME_COLS, dtype=str)


def biz(df):
    return df[BIZ_COLS].reset_index(drop=True)


def write_base(root, ind, rows, rid, smid):
    """Base file made by hand: rows -> 14-col psv with one-element lineages."""
    df = stg_df(rows, rid, smid, ind, cols=STAGING_COLS)
    df["request_id_linage"] = rid
    df["staging_merged_id_linage"] = smid
    df = df[BASE_COLS]
    psv_write(base_path(root, ind), df)
    return df


def series_rows(series):
    """{country: {year: value}} -> staging-style rows"""
    return [{"countryiso3code": c, "date": y, "value": v}
            for c, ys in series.items() for y, v in ys.items()]


def find_exc(name):
    """Locate an exception class by name in any src module (plan lists FileCollisionError under
    'io_utils/ingest/archive', so its home module is not pinned)."""
    import importlib
    import pkgutil

    import src
    for info in pkgutil.iter_modules(src.__path__):
        mod = importlib.import_module(f"src.{info.name}")
        cls = getattr(mod, name, None)
        if isinstance(cls, type):
            return cls
    raise ImportError(f"{name} not defined in any src module")


def frames_equal(a, b):
    """dtype-agnostic equality of two all-str frames (pandas may use object or str dtype)."""
    return (list(a.columns) == list(b.columns)
            and a.reset_index(drop=True).astype(object).values.tolist()
            == b.reset_index(drop=True).astype(object).values.tolist())
