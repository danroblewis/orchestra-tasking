"""The store's files: JSON documents written whole, under one file lock."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def read(path: Path, empty: dict) -> dict:
    if not path.exists():
        return json.loads(json.dumps(empty))
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else json.loads(json.dumps(empty))


def write(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def locked(lock_file: Path) -> Iterator[None]:
    """One lock around every read-modify-write, across the scheduler and the web server."""
    with lock_file.open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
