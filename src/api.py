"""URL building, request ids, page fetching with retry, and full pull of one (country, indicator)."""
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from .errors import ApiHttpError, RateLimitExhausted, RetryExhausted, SchemaCheckError  # noqa: F401
from .config import get_indicator
from .schema_check import schema_check


def _utc_now():
    return datetime.now(timezone.utc)


def build_url(cfg, country, indicator_code, page):
    api = cfg["api"]
    path = api["path_template"].format(country=country, indicator=indicator_code)
    params = [("format", api.get("format", "json")),
              ("date", f"{cfg['date_range']['start']}:{cfg['date_range']['end']}")]
    if api.get("per_page") is not None:
        params.append(("per_page", api["per_page"]))
    params.append(("page", page))
    return f"{api['base_url'].rstrip('/')}/{path.lstrip('/')}?{urlencode(params, safe=':')}"


def generate_request_id(cfg, *, now=_utc_now):
    return now().strftime(cfg["request_id"]["format"])


def fetch_page(url, cfg, *, http_get=requests.get, sleep=time.sleep):
    retry = cfg["retry"]
    max_retries = retry["max_retries"]
    retries = 0
    while True:
        cause, status = None, None
        try:
            resp = http_get(url, timeout=cfg["api"]["timeout_seconds"])
        except (requests.ConnectionError, requests.Timeout) as e:
            if not retry["retry_on_connection_errors"]:
                raise ApiHttpError(f"connection error for GET {url}: {e}") from e
            cause = f"{type(e).__name__}: {e}"
        except requests.RequestException as e:
            raise ApiHttpError(f"request failed for GET {url}: {e}") from e
        else:
            status = resp.status_code
            retryable = status in retry["status_codes"] or (retry["retry_on_5xx"] and 500 <= status <= 599)
            if retryable:
                cause = f"HTTP {status}"
            elif status != 200:
                raise ApiHttpError(f"HTTP {status} for GET {url}")
            else:
                try:
                    return resp.json()
                except ValueError as e:
                    raise ApiHttpError(f"HTTP 200 with non-JSON body for GET {url}") from e
        if retries >= max_retries:
            attempts = retries + 1
            if status == 429:
                raise RateLimitExhausted(
                    f"HTTP 429 Too Many Requests persisted after {max_retries} retries "
                    f"({attempts} attempts) for GET {url}; pipeline stopped",
                    url=url, attempts=attempts, last_cause=cause)
            raise RetryExhausted(
                f"{cause} persisted after {max_retries} retries ({attempts} attempts) for GET {url}; "
                "pipeline stopped", url=url, attempts=attempts, last_cause=cause)
        retries += 1
        sleep(retry["sleep_seconds"])


def flatten_pk_values(pages, pk):
    """Primary-key tuples of every record of the pull (raw records)."""
    out = []
    for page in pages:
        for rec in page[1]:
            flat = {"countryiso3code": rec["countryiso3code"], "date": rec["date"],
                    "indicator_id": rec["indicator"]["id"], "indicator_name": rec["indicator"]["value"],
                    "country_id": rec["country"]["id"], "country_name": rec["country"]["value"],
                    "value": rec["value"], "unit": rec["unit"], "obs_status": rec["obs_status"],
                    "decimal": rec["decimal"]}
            out.append(tuple(flat[c] for c in pk))
    return out


def fetch_pull(cfg, country, indicator_code, request_id, *, http_get=requests.get, sleep=time.sleep):
    try:
        return _fetch_pull(cfg, country, indicator_code, request_id, http_get, sleep)
    except RetryExhausted as e:
        hint = f" Rerun to resume at country={country} indicator={indicator_code}."
        raise type(e)(f"{e} [country={country} indicator={indicator_code}]{hint}", url=e.url,
                      attempts=e.attempts, last_cause=e.last_cause) from e
    except ApiHttpError as e:
        raise ApiHttpError(f"{e} [country={country} indicator={indicator_code}]. "
                           f"Rerun to resume at country={country} indicator={indicator_code}.") from e


def _fetch_pull(cfg, country, indicator_code, request_id, http_get, sleep):
    pages = []
    first = None
    n, total_pages = 1, 1
    while n <= total_pages:
        resp = fetch_page(build_url(cfg, country, indicator_code, n), cfg, http_get=http_get, sleep=sleep)
        schema_check(resp, cfg, country, indicator_code, n)
        meta = resp[0]
        if first is None:
            first = meta
            total_pages = meta["pages"]
        else:
            errs = []
            if meta["pages"] != first["pages"]:
                errs.append(f"pages changed from {first['pages']} to {meta['pages']} on page {n}")
            if meta["total"] != first["total"]:
                errs.append(f"total changed from {first['total']} to {meta['total']} on page {n}")
            if errs:
                raise SchemaCheckError(
                    f"cross-page inconsistency for country={country} indicator={indicator_code} "
                    f"request_id={request_id}: " + "; ".join(errs), errs)
        pages.append(resp)
        n += 1
    pk = get_indicator(cfg, indicator_code)["primary_key"]
    keys = flatten_pk_values(pages, pk)
    seen, dups = set(), []
    for k in keys:
        if k in seen:
            dups.append(k)
        seen.add(k)
    if dups:
        errs = [f"duplicate primary key {pk} = {d} across pages" for d in dups]
        raise SchemaCheckError(
            f"duplicate primary keys in pull country={country} indicator={indicator_code} "
            f"request_id={request_id}: " + "; ".join(errs), errs)
    return pages
