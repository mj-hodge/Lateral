"""No network / no secrets, and nothing hardcoded in src (prompt: 'Nothing is hardcoded')."""
import ast
import os
import socket
from pathlib import Path

import pytest
import requests

SRC = Path(__file__).resolve().parents[1] / "src"
HARDCODED = ["api.worldbank.org", "worldbank.org", "SP.POP.TOTL", "NY.GDP.MKTP.CD", "NY.GDP.PCAP.CD",
             "SP.DYN.LE00.IN", "BRA", "CAN", "DEU", "GBR", "IND", "JPN", "MEX", "USA", "ZAF",
             "storage/", "sql/code", "sql/analysis", "config/pipeline"]


def src_files():
    files = sorted(SRC.rglob("*.py"))
    assert files, "src/ contains no python modules"
    return files


def string_constants(tree):
    """String literals that are not docstrings."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.value


# ----------------------------------------------------------------------------- network guard works
def test_network_guard_blocks_requests_and_sockets():
    with pytest.raises(AssertionError):
        requests.get("https://api.worldbank.org/v2/country/BRA/indicator/SP.POP.TOTL?format=json")
    with pytest.raises(AssertionError):
        socket.create_connection(("api.worldbank.org", 443))
    with pytest.raises(AssertionError):
        socket.getaddrinfo("api.worldbank.org", 443)
    with pytest.raises(AssertionError):
        requests.Session().get("https://api.worldbank.org/")


def test_pipeline_modules_do_not_read_environment_secrets():
    offenders = []
    for f in src_files():
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv") \
                    and isinstance(node.value, ast.Name) and node.value.id == "os":
                offenders.append(f"{f.name}:{node.lineno}")
    assert offenders == []


def test_requests_get_is_only_a_default_for_the_injectable_seam():
    """Any use of requests.get other than as a keyword default would bypass injection."""
    offenders = []
    for f in src_files():
        tree = ast.parse(f.read_text())
        defaults = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
                    defaults.add(id(d))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("get", "post", "request", "Session") \
                    and isinstance(node.value, ast.Name) and node.value.id == "requests" and id(node) not in defaults:
                offenders.append(f"{f.name}:{node.lineno}")
    assert offenders == []


def test_time_sleep_is_only_a_default_for_the_injectable_seam():
    offenders = []
    for f in src_files():
        tree = ast.parse(f.read_text())
        defaults = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
                    defaults.add(id(d))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "sleep" and isinstance(node.value, ast.Name) \
                    and node.value.id == "time" and id(node) not in defaults:
                offenders.append(f"{f.name}:{node.lineno}")
    assert offenders == []


# ----------------------------------------------------------------------------- nothing hardcoded
@pytest.mark.parametrize("needle", HARDCODED)
def test_no_hardcoded_config_values_in_src(needle):
    hits = []
    for f in src_files():
        for s in string_constants(ast.parse(f.read_text())):
            if needle in s:
                hits.append(f"{f.name}: {s!r}")
    assert hits == []


def test_no_hardcoded_years_in_src():
    hits = []
    for f in src_files():
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Constant) and node.value in (2018, 2024, "2018", "2024", "2018:2024"):
                hits.append(f"{f.name}:{node.lineno}")
    assert hits == []


def test_tests_directory_keeps_generated_reports_separate_from_pytest_files():
    tests_dir = Path(__file__).resolve().parent
    for p in tests_dir.rglob("*.psv"):
        assert p.parent.name == "row_count"
    for p in tests_dir.glob("test_*.py"):
        assert p.is_file()
