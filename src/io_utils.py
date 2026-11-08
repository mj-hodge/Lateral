"""Atomic text writes and pipe-delimited (psv) read/write helpers."""
import os
import uuid
from pathlib import Path

import pandas as pd

from .errors import FileCollisionError  # noqa: F401  (re-export)


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_psv(df, path):
    for col in df.columns:
        for v in df[col].tolist():
            if isinstance(v, str) and any(ch in v for ch in "|\n\r"):
                raise ValueError(f"column {col!r} contains a pipe or newline: {v!r}")
    text = df.to_csv(sep="|", index=False, lineterminator="\n", na_rep="")
    atomic_write_text(path, text)


def read_psv(path):
    return pd.read_csv(path, sep="|", dtype=str, keep_default_na=False)
