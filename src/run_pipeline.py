"""Pipeline orchestration and command line entrypoint."""
import argparse
import logging
import sys
import time
from pathlib import Path

import requests

from . import api
from .analysis import run_analysis
from .archive import archive_landing
from .config import get_indicator, load_config, resolve_path
from .dq_tests import assert_row_count_passed, row_count_test, write_row_count_report
from .errors import PipelineError, StagingError
from .ingest import ingest_indicator, mark_staging_failed
from .merge import merge_indicator, merge_leftover_staging
from .staging import build_staging

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def run_indicator(cfg, indicator_code, *, refetch=False, http_get=requests.get, sleep=time.sleep,
                  now=api._utc_now):
    get_indicator(cfg, indicator_code)
    log.info("indicator %s: start", indicator_code)
    leftover = merge_leftover_staging(cfg, indicator_code)
    pulled = ingest_indicator(cfg, indicator_code, refetch=refetch, http_get=http_get, sleep=sleep, now=now)
    try:
        staging_df, frames, smid, staging_path = build_staging(cfg, indicator_code)
    except StagingError as e:
        if e.request_ids:
            mark_staging_failed(cfg, indicator_code, e.request_ids)
        raise
    results = row_count_test(cfg, indicator_code, frames)
    report = write_row_count_report(cfg, indicator_code, results, smid)
    assert_row_count_passed(results)
    archived = archive_landing(cfg, indicator_code)
    base_path = merge_indicator(cfg, indicator_code, staging_path)
    log.info("indicator %s: merged %d staging rows into %s", indicator_code, len(staging_df), base_path)
    return {"indicator": indicator_code, "pulled": len(pulled), "staging_merge_id": smid,
            "staging_path": staging_path, "report": report, "archived": archived,
            "base_path": base_path, "leftover_merged": leftover}


def run_all(cfg, *, indicators=None, skip_analysis=False, refetch=False, http_get=requests.get,
            sleep=time.sleep, now=api._utc_now):
    codes = list(indicators) if indicators is not None else [i["code"] for i in cfg["indicators"]]
    summaries = []
    for code in codes:
        summaries.append(run_indicator(cfg, code, refetch=refetch, http_get=http_get, sleep=sleep, now=now))
    out = None
    if not skip_analysis:
        out = run_analysis(cfg)
        log.info("analysis written to %s", out)
    return {"indicators": summaries, "analysis": out}


def _parse_args(argv):
    default_cfg = Path(__file__).resolve().parent.parent / "config" / "pipeline.yaml"
    p = argparse.ArgumentParser(prog="run_pipeline", description="World Bank indicator pipeline")
    p.add_argument("--config", default=str(default_cfg), help="path to the pipeline YAML config")
    p.add_argument("--indicator", nargs="+", action="extend", default=None,
                   help="only run these indicator codes (default: all, in config order)")
    p.add_argument("--skip-analysis", action="store_true", help="do not run the SQL analysis")
    p.add_argument("--refetch", action="store_true", help="ignore resume; re-pull every pair")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv=None, *, http_get=requests.get, sleep=time.sleep, now=api._utc_now):
    args = _parse_args(argv)
    level = getattr(logging, str(args.log_level).upper(), logging.INFO)
    root = logging.getLogger()
    handlers = []
    old_level = root.level

    def add(h):
        h.setFormatter(logging.Formatter(LOG_FORMAT))
        h.setLevel(level)
        root.addHandler(h)
        handlers.append(h)

    add(logging.StreamHandler(sys.stderr))
    root.setLevel(min(old_level, level) if old_level else level)
    try:
        try:
            cfg = load_config(args.config)
            run_log = resolve_path(cfg, "run_log")
            run_log.parent.mkdir(parents=True, exist_ok=True)
            add(logging.FileHandler(run_log, encoding="utf-8"))
            run_all(cfg, indicators=args.indicator, skip_analysis=args.skip_analysis,
                    refetch=args.refetch, http_get=http_get, sleep=sleep, now=now)
            return 0
        except PipelineError as e:
            log.error("%s: %s", type(e).__name__, e)
            return e.exit_code
        except Exception:
            log.exception("unexpected error")
            return 1
    finally:
        for h in handlers:
            root.removeHandler(h)
            h.close()
        root.setLevel(old_level)


if __name__ == "__main__":
    sys.exit(main())
