"""DuckDB analysis: load base tables, run the configured SQL file, write the result as psv."""
import logging
import re

import duckdb

from .config import resolve_path
from .errors import AnalysisError
from .io_utils import write_psv

log = logging.getLogger(__name__)


def _quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def _ident(s):
    return '"' + str(s).replace('"', '""') + '"'


def load_base_tables(con, cfg):
    tables = {}
    for ind in cfg["indicators"]:
        path = resolve_path(cfg, "data", ind["code"]) / f"{ind['code']}.psv"
        if not path.exists():
            raise AnalysisError(f"base table file missing for indicator {ind['code']}: {path}")
        try:
            con.execute(f"CREATE OR REPLACE TABLE {_ident(ind['table_name'])} AS SELECT * FROM "
                        f"read_csv({_quote(path)}, delim='|', header=true, all_varchar=true)")
        except duckdb.Error as e:
            raise AnalysisError(f"cannot load {path}: {e}") from e
        tables[ind["code"]] = ind["table_name"]
    for alias, code in cfg["analysis"]["tables"].items():
        try:
            con.execute(f"CREATE OR REPLACE VIEW {_ident(alias)} AS SELECT * FROM {_ident(tables[code])}")
        except duckdb.Error as e:
            raise AnalysisError(f"cannot create view {alias}: {e}") from e


def run_analysis(cfg):
    sql_path = resolve_path(cfg, "analysis_sql")
    if not sql_path.exists():
        raise AnalysisError(f"analysis SQL file missing: {sql_path}")
    sql = sql_path.read_text(encoding="utf-8")
    params = {"start_year": int(cfg["date_range"]["start"]), "end_year": int(cfg["date_range"]["end"])}
    # DuckDB rejects named parameters the statement does not reference, so bind only the used ones.
    params = {k: v for k, v in params.items() if re.search(r"\$" + k + r"\b", sql)}
    con = duckdb.connect()
    try:
        load_base_tables(con, cfg)
        try:
            df = con.execute(sql, params).df() if params else con.execute(sql).df()
        except duckdb.Error as e:
            raise AnalysisError(f"analysis query failed: {e}") from e
    finally:
        con.close()
    out = resolve_path(cfg, "analysis_out") / cfg["analysis"]["output_file"]
    write_psv(df, out)
    expected = len(cfg["countries"])
    if len(df) != expected:
        log.warning("analysis returned %d rows for %d configured countries", len(df), expected)
    return out
