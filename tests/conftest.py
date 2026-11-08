"""Shared fixtures: network guard, fake API/sleep/clock, config written under tmp_path.

pytest.ini sets --basetemp=.pytest_tmp so tmp_path lives inside the project; the session-finish
hook below deletes that directory (plan sections 4 and 15).
"""
import shutil
import socket
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make `helpers` importable regardless of import mode

import helpers  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(REPO_ROOT / ".pytest_tmp", ignore_errors=True)


@pytest.fixture(autouse=True)
def network_guard(monkeypatch):
    """Any real network access fails the test (plan section 15, no network/secrets)."""
    def boom(*a, **k):
        raise AssertionError("real network access attempted in a unit test")
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(requests.Session, "send", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture
def fake_sleep():
    return helpers.FakeSleep()


@pytest.fixture
def fake_now():
    return helpers.FakeNow()


@pytest.fixture
def fake_api():
    """FakeHttpGet that serves a standard 3-page pull for any (country, indicator)."""
    return helpers.FakeHttpGet()


@pytest.fixture
def make_api():
    return helpers.FakeHttpGet


@pytest.fixture
def cfg_dict():
    return helpers.base_cfg_dict()


@pytest.fixture
def root(tmp_path):
    return tmp_path


@pytest.fixture
def cfg_path(tmp_path, cfg_dict):
    return helpers.write_cfg(tmp_path, cfg_dict)


@pytest.fixture
def cfg(cfg_path):
    from src.config import load_config
    return load_config(cfg_path)


@pytest.fixture
def load_cfg(tmp_path):
    """Write a (possibly modified) config dict under tmp_path and load it."""
    def _load(d, rel="config/pipeline.yaml"):
        from src.config import load_config
        return load_config(helpers.write_cfg(tmp_path, d, rel))
    return _load


@pytest.fixture
def real_sql(tmp_path):
    """Copy the repository's real sql/code/analysis.sql into the test project root."""
    src = REPO_ROOT / "sql" / "code" / "analysis.sql"
    assert src.exists(), "sql/code/analysis.sql must exist in the repository"
    dst = tmp_path / "sql" / "code" / "analysis.sql"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst
