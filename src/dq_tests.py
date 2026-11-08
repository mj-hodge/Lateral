"""Data quality tests: row_count."""
from datetime import datetime, timezone

import pandas as pd

from .config import resolve_path
from .errors import RowCountFailure
from .io_utils import write_psv

REPORT_COLS = ["request_id", "country_code", "indicator_code", "expected_total", "actual_rows",
               "status", "detail", "staging_merge_id", "checked_at"]


def read_request_log(cfg, indicator_code, request_id):
    path = resolve_path(cfg, "request_log", indicator_code) / f"{request_id}.log"
    text = path.read_text(encoding="utf-8")  # FileNotFoundError when missing
    line = text.rstrip("\n")
    parts = line.split("|")
    if len(parts) != 5:
        raise ValueError(f"malformed request log {path}: expected 5 fields, got {len(parts)}")
    rid, total, pages, country, ind = parts
    if rid != request_id:
        raise ValueError(f"request log {path}: request_id field {rid!r} != file name {request_id!r}")
    try:
        return {"request_id": rid, "total": int(total), "pages": int(pages), "country": country,
                "indicator": ind}
    except ValueError:
        raise ValueError(f"malformed request log {path}: total/pages must be integers")


def row_count_test(cfg, indicator_code, frames):
    results = []
    for rid, item in frames.items():
        country, actual = item["country"], len(item["frame"])
        res = {"request_id": rid, "country_code": country, "indicator_code": indicator_code,
               "expected_total": "", "actual_rows": actual, "status": "FAIL", "detail": ""}
        try:
            entry = read_request_log(cfg, indicator_code, rid)
        except (OSError, ValueError) as e:
            res["detail"] = f"request log unusable: {e}"
        else:
            res["expected_total"] = entry["total"]
            if entry["country"] != country:
                res["detail"] = f"log country {entry['country']} != landing country {country}"
            elif entry["total"] != actual:
                res["detail"] = f"log total {entry['total']} != dataframe rows {actual}"
            else:
                res["status"] = "PASS"
        results.append(res)
    return results


def write_row_count_report(cfg, indicator_code, results, staging_merge_id):
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S%f")
    checked = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [[str(r["request_id"]), str(r["country_code"]), str(r["indicator_code"]),
             str(r["expected_total"]), str(r["actual_rows"]), str(r["status"]),
             str(r.get("detail", "") or "").replace("|", "/").replace("\n", " ").replace("\r", " "),
             staging_merge_id, checked] for r in results]
    path = resolve_path(cfg, "row_count_reports", indicator_code) / f"row_count_{ts}_{staging_merge_id}.psv"
    write_psv(pd.DataFrame(rows, columns=REPORT_COLS, dtype=str), path)
    return path


def latest_report(cfg, indicator_code):
    d = resolve_path(cfg, "row_count_reports", indicator_code)
    if not d.is_dir():
        return None
    files = sorted(p for p in d.glob("row_count_*.psv") if p.is_file())
    return files[-1] if files else None


def assert_row_count_passed(results):
    failed = [r for r in results if r["status"] != "PASS"]
    if failed:
        raise RowCountFailure(
            "row_count FAILED for " + "; ".join(
                f"{r['request_id']} ({r.get('country_code')}): {r.get('detail', '')}" for r in failed),
            failed)
