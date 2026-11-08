"""io_utils: atomic_write_text, write_psv, read_psv (plan section 5)."""
import os

import pandas as pd
import pytest

from helpers import frames_equal
from src import io_utils


def test_atomic_write_text_creates_parents_and_content(tmp_path):
    target = tmp_path / "a" / "b" / "c.txt"
    io_utils.atomic_write_text(target, "hello\n")
    assert target.read_text() == "hello\n"
    assert sorted(p.name for p in target.parent.iterdir()) == ["c.txt"]  # no temp file left behind


def test_atomic_write_text_overwrites(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("old")
    io_utils.atomic_write_text(target, "new")
    assert target.read_text() == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.txt"]


def test_atomic_write_text_uses_os_replace(tmp_path, monkeypatch):
    calls = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: (calls.append((str(s), str(d))), real(s, d))[1])
    target = tmp_path / "f.txt"
    io_utils.atomic_write_text(target, "x")
    assert calls and calls[-1][1] == str(target)
    assert calls[-1][0] != str(target)  # written under a temp name first


def test_write_psv_format(tmp_path):
    df = pd.DataFrame({"a": ["1", "x y", None], "b": ["", "2", "3"]}, dtype=object)
    path = tmp_path / "t.psv"
    io_utils.write_psv(df, path)
    assert path.read_bytes() == b"a|b\n1|\nx y|2\n|3\n"


def test_write_psv_no_index_no_temp_files(tmp_path):
    df = pd.DataFrame({"a": ["1"]}, index=[7])
    path = tmp_path / "t.psv"
    io_utils.write_psv(df, path)
    assert path.read_text() == "a\n1\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t.psv"]


def test_write_psv_overwrites_existing(tmp_path):
    path = tmp_path / "t.psv"
    path.write_text("old\n")
    io_utils.write_psv(pd.DataFrame({"a": ["new"]}), path)
    assert path.read_text() == "a\nnew\n"


@pytest.mark.parametrize("bad", ["a|b", "a\nb", "a\rb", "a\r\nb"])
def test_write_psv_rejects_pipe_or_newline_and_keeps_old_file(tmp_path, bad):
    path = tmp_path / "t.psv"
    path.write_text("keep\n")
    with pytest.raises(ValueError):
        io_utils.write_psv(pd.DataFrame({"a": ["ok", bad]}), path)
    assert path.read_text() == "keep\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t.psv"]


def test_read_psv_reads_everything_as_strings(tmp_path):
    path = tmp_path / "t.psv"
    path.write_text("a|b|c\n007|NA|\n1.50|null|x\n")
    df = io_utils.read_psv(path)
    assert list(df.columns) == ["a", "b", "c"]
    assert df.values.tolist() == [["007", "NA", ""], ["1.50", "null", "x"]]
    assert all(isinstance(v, str) for v in df.to_numpy().ravel())


def test_psv_roundtrip(tmp_path):
    df = pd.DataFrame({"k": ["BRA", "CAN"], "v": ["2185821610689.31", ""]})
    path = tmp_path / "t.psv"
    io_utils.write_psv(df, path)
    back = io_utils.read_psv(path)
    assert frames_equal(back, df)
