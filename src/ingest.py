"""Ingestion: per country+indicator pulls, landing page files, request log, recovery rules."""
import json
import logging
import os
import re
import shutil
import time
import uuid

import requests

from . import api, dq_tests
from .config import resolve_path
from .errors import FileCollisionError, StagingError
from .io_utils import atomic_write_text, read_psv

log = logging.getLogger(__name__)

LANDING_RE = re.compile(r"^(?P<C>[A-Z]{3})_(?P<I>.+)_(?P<R>[^_]+)_p(?P<N>\d+)\.json$")


# ----------------------------------------------------------------------------- naming
def landing_filename(country, indicator_code, request_id, page):
    return f"{country}_{indicator_code}_{request_id}_p{page}.json"


def format_log_line(request_id, total, pages, country, indicator_code):
    return f"{request_id}|{total}|{pages}|{country}|{indicator_code}"


def _log_path(cfg, request_id, indicator_code):
    return resolve_path(cfg, "request_log", indicator_code) / f"{request_id}.log"


def write_request_log(cfg, request_id, total, pages, country, indicator_code):
    path = _log_path(cfg, request_id, indicator_code)
    atomic_write_text(path, format_log_line(request_id, total, pages, country, indicator_code) + "\n")
    return path


# ----------------------------------------------------------------------------- landing files
def write_landing_pages(cfg, country, indicator_code, request_id, pages):
    d = resolve_path(cfg, "landing", indicator_code)
    finals = [d / landing_filename(country, indicator_code, request_id, n) for n in range(1, len(pages) + 1)]
    for f in finals:
        if f.exists():
            raise FileCollisionError(f"landing file already exists: {f}")
    texts = [json.dumps(p) for p in pages]  # serialize everything before touching the disk
    d.mkdir(parents=True, exist_ok=True)
    parts = []
    try:
        for f, text in zip(finals, texts):
            part = f.with_name(f.name + ".part")
            parts.append(part)
            part.write_text(text, encoding="utf-8")
        for part, f in zip(parts, finals):
            os.replace(part, f)
    except BaseException:
        for part in parts:
            if part.exists():
                part.unlink()
        raise
    return finals


def _remove_part_files(cfg, indicator_code):
    d = resolve_path(cfg, "landing", indicator_code)
    if d.is_dir():
        for p in d.glob("*.part"):
            p.unlink()


def _scan_landing(cfg, indicator_code):
    """{country: {request_id: [paths sorted by page number]}} for the indicator's landing dir."""
    d = resolve_path(cfg, "landing", indicator_code)
    out = {}
    if not d.is_dir():
        return out
    for p in d.iterdir():
        if not p.is_file():
            continue
        m = LANDING_RE.match(p.name)
        if not m or m.group("I") != indicator_code:
            continue
        out.setdefault(m.group("C"), {}).setdefault(m.group("R"), []).append((int(m.group("N")), p))
    return {c: {r: [p for _, p in sorted(v)] for r, v in sorted(g.items())} for c, g in out.items()}


def find_existing_landing(cfg, indicator_code, country):
    return _scan_landing(cfg, indicator_code).get(country, {})


def check_page_set(pages_by_number, request_id=None):
    rids = [request_id] if request_id else []

    def bad(msg):
        raise StagingError(msg + (f" (request_id {request_id})" if request_id else ""), rids)

    if not pages_by_number:
        bad("empty page set")
    numbers = sorted(pages_by_number)
    first = pages_by_number[numbers[0]]
    try:
        pages, total = first[0]["pages"], first[0]["total"]
    except (KeyError, TypeError, IndexError):
        bad("unreadable page metadata")
    if numbers != list(range(1, pages + 1)):
        bad(f"page numbers {numbers} are not exactly 1..{pages}")
    for n in numbers:
        try:
            meta = pages_by_number[n][0]
            ok = meta["pages"] == pages and meta["total"] == total and meta["page"] == n
        except (KeyError, TypeError, IndexError):
            ok = False
        if not ok:
            bad(f"page file {n} disagrees with the set (pages={pages}, total={total}) or its own page number")


# ----------------------------------------------------------------------------- moves
def _files_of_request(cfg, indicator_code, request_id):
    out = []
    for g in _scan_landing(cfg, indicator_code).values():
        out.extend(g.get(request_id, []))
    return sorted(out, key=lambda p: (int(LANDING_RE.match(p.name).group("N")), p.name))


def move_request_files(cfg, indicator_code, request_id, dest_key):
    files = _files_of_request(cfg, indicator_code, request_id)
    dest = resolve_path(cfg, dest_key, indicator_code)
    for f in files:
        if (dest / f.name).exists():
            raise FileCollisionError(f"target already exists: {dest / f.name}")
    if files:
        dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for f in files:
        target = dest / f.name
        shutil.move(str(f), str(target))
        moved.append(target)
    return moved


# ----------------------------------------------------------------------------- re-pull marker
def _marker(cfg, indicator_code):
    return resolve_path(cfg, "staging_failed_marker", indicator_code)


def _read_marker(cfg, indicator_code):
    p = _marker(cfg, indicator_code)
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text().splitlines() if ln.strip()}


def mark_staging_failed(cfg, indicator_code, request_ids):
    ids = _read_marker(cfg, indicator_code) | set(request_ids)
    atomic_write_text(_marker(cfg, indicator_code), "".join(f"{i}\n" for i in sorted(ids)))


def clear_staging_marks(cfg, indicator_code, request_ids):
    p = _marker(cfg, indicator_code)
    if not p.exists():
        return
    ids = _read_marker(cfg, indicator_code) - set(request_ids)
    if ids:
        atomic_write_text(p, "".join(f"{i}\n" for i in sorted(ids)))
    else:
        p.unlink()


def failed_request_ids(cfg, indicator_code):
    ids = set(_read_marker(cfg, indicator_code))
    report = dq_tests.latest_report(cfg, indicator_code)
    if report is not None:
        df = read_psv(report)
        if {"request_id", "status"} <= set(df.columns):
            ids |= set(df.loc[df["status"] == "FAIL", "request_id"])
    return ids


def _reject(cfg, indicator_code, request_id):
    moved = move_request_files(cfg, indicator_code, request_id, "rejected")
    clear_staging_marks(cfg, indicator_code, [request_id])
    return moved


# ----------------------------------------------------------------------------- pulls
def ingest_pair(cfg, country, indicator_code, *, http_get=requests.get, sleep=time.sleep,
                now=api._utc_now):
    request_id = api.generate_request_id(cfg, now=now)
    lp = _log_path(cfg, request_id, indicator_code)
    if lp.exists():
        raise FileCollisionError(f"request log already exists: {lp}")
    pages = api.fetch_pull(cfg, country, indicator_code, request_id, http_get=http_get, sleep=sleep)
    total, npages = pages[0][0]["total"], pages[0][0]["pages"]
    # collision check on page files happens before anything is written
    d = resolve_path(cfg, "landing", indicator_code)
    for n in range(1, len(pages) + 1):
        f = d / landing_filename(country, indicator_code, request_id, n)
        if f.exists():
            raise FileCollisionError(f"landing file already exists: {f}")
    log_path = write_request_log(cfg, request_id, total, npages, country, indicator_code)
    try:
        paths = write_landing_pages(cfg, country, indicator_code, request_id, pages)
    except BaseException:
        if log_path.exists():
            log_path.unlink()
        raise
    return {"request_id": request_id, "total": total, "pages": npages,
            "landing_paths": paths, "log_path": log_path}


def _group_is_complete(paths):
    pages = {}
    for p in paths:
        try:
            resp = json.loads(p.read_text(encoding="utf-8"))
            pages[int(LANDING_RE.match(p.name).group("N"))] = resp
        except (OSError, ValueError):
            return False
    try:
        check_page_set(pages)
    except StagingError:
        return False
    return True


def ingest_indicator(cfg, indicator_code, *, refetch=False, http_get=requests.get, sleep=time.sleep,
                     now=api._utc_now):
    _remove_part_files(cfg, indicator_code)
    failed = failed_request_ids(cfg, indicator_code)
    groups = _scan_landing(cfg, indicator_code)
    for country, g in groups.items():
        if country not in cfg["countries"]:
            for rid in g:
                log.warning("landing files of %s (request_id %s) are for a country no longer in the "
                            "config; moving to rejected", country, rid)
                _reject(cfg, indicator_code, rid)
    results = []
    for country in cfg["countries"]:
        existing = find_existing_landing(cfg, indicator_code, country)
        rids = list(existing)
        newest = rids[-1] if rids else None
        for rid in rids[:-1] if newest and not refetch else []:
            _reject(cfg, indicator_code, rid)
        if newest and not refetch:
            if newest not in failed and _group_is_complete(existing[newest]):
                log.info("resume: %s %s already landed as %s", country, indicator_code, newest)
                continue
            old = [newest]
        else:
            old = rids
        res = ingest_pair(cfg, country, indicator_code, http_get=http_get, sleep=sleep, now=now)
        for rid in old:
            _reject(cfg, indicator_code, rid)
        results.append(res)
    return results
