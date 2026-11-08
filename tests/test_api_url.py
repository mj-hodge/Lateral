"""URL building from config and request_id generation (plan sections 5 and 6)."""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from helpers import GDP, LE, POP, START_NOW, base_cfg_dict, write_cfg
from src import api


def parts(url):
    u = urlparse(url)
    q = {k: v for k, v in parse_qs(u.query, keep_blank_values=True).items()}
    return u, q


def test_build_url_from_config(cfg):
    u, q = parts(api.build_url(cfg, "BRA", LE, 1))
    assert u.scheme == "https" and u.netloc == "api.worldbank.org"
    assert u.path == f"/v2/country/BRA/indicator/{LE}"
    assert q["format"] == ["json"]
    assert q["date"] == ["2018:2024"]
    assert q["page"] == ["1"]
    assert q["per_page"] == ["3"]


def test_build_url_page_number(cfg):
    for page in (1, 2, 3, 10):
        _, q = parts(api.build_url(cfg, "CAN", GDP, page))
        assert q["page"] == [str(page)]


def test_build_url_returns_str(cfg):
    assert isinstance(api.build_url(cfg, "BRA", LE, 1), str)


def test_build_url_per_page_omitted_when_null(tmp_path):
    d = base_cfg_dict()
    d["api"]["per_page"] = None
    from src.config import load_config
    cfg = load_config(write_cfg(tmp_path, d))
    _, q = parts(api.build_url(cfg, "BRA", LE, 2))
    assert "per_page" not in q
    assert q["page"] == ["2"]


def test_build_url_follows_config_changes(tmp_path):
    from src.config import load_config
    d = base_cfg_dict()
    d["api"].update(base_url="https://example.test/api/v9", path_template="c/{country}/i/{indicator}/data",
                    per_page=50, format="json")
    d["date_range"] = {"start": 2000, "end": 2003}
    cfg = load_config(write_cfg(tmp_path, d))
    u, q = parts(api.build_url(cfg, "CAN", GDP, 4))
    assert u.netloc == "example.test"
    assert u.path == f"/api/v9/c/CAN/i/{GDP}/data"
    assert q["date"] == ["2000:2003"]
    assert q["per_page"] == ["50"]
    assert q["page"] == ["4"]


def test_build_url_new_country_and_indicator_need_no_code_change(tmp_path):
    from src.config import load_config
    d = base_cfg_dict()
    d["countries"].append("ZAF")
    d["indicators"].append({"code": POP, "name": "Population, total", "table_name": "population_total",
                            "primary_key": ["countryiso3code", "date"]})
    cfg = load_config(write_cfg(tmp_path, d))
    u, _ = parts(api.build_url(cfg, "ZAF", POP, 1))
    assert u.path == f"/v2/country/ZAF/indicator/{POP}"


# ----------------------------------------------------------------------------- generate_request_id
def test_generate_request_id_exact_value_and_shape(cfg):
    rid = api.generate_request_id(cfg, now=lambda: START_NOW)
    assert rid == "20261108T153045123456"
    assert len(rid) == 21
    assert re.fullmatch(r"\d{8}T\d{12}", rid)


def test_generate_request_id_zero_padded_microseconds(cfg):
    now = datetime(2026, 1, 2, 3, 4, 5, 7, tzinfo=timezone.utc)
    assert api.generate_request_id(cfg, now=lambda: now) == "20260102T030405000007"


def test_generate_request_id_default_clock(cfg):
    rid = api.generate_request_id(cfg)
    assert re.fullmatch(r"\d{8}T\d{12}", rid)


def test_generate_request_id_calls_now_once(cfg, fake_now):
    api.generate_request_id(cfg, now=fake_now)
    assert fake_now.count == 1


def test_successive_clock_values_give_distinct_sortable_ids(cfg, fake_now):
    ids = [api.generate_request_id(cfg, now=fake_now) for _ in range(5)]
    assert len(set(ids)) == 5
    assert ids == sorted(ids)


def test_generate_request_id_uses_config_format(tmp_path):
    from src.config import load_config
    d = base_cfg_dict()
    d["request_id"]["format"] = "%Y%m%d%H%M%S%f"
    cfg = load_config(write_cfg(tmp_path, d))
    assert api.generate_request_id(cfg, now=lambda: START_NOW) == "20261108153045123456"
    d["request_id"]["format"] = "%Y%m%d-%H-%M-%S-%f"
    cfg = load_config(write_cfg(tmp_path, d))
    assert api.generate_request_id(cfg, now=lambda: START_NOW) == "20261108-15-30-45-123456"
