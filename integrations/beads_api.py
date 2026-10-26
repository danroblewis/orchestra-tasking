"""Reading Beads' HTTP API (bd serve): the event journal and counts. Read-only."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

URL = os.environ.get("BEADS_URL", "http://127.0.0.1:7811/v0/beads")


def get(path: str, **query) -> dict:
    url = URL + path + ("?" + urllib.parse.urlencode(query) if query else "")
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def events(since: int, limit: int = 500) -> tuple[list[dict], int]:
    """Journal records after ``since`` (by seq), and the journal's head."""
    d = get("/events", since=since, limit=limit)
    return d.get("records") or [], int(d.get("head") or 0)


def count(**query) -> int:
    return int(get("/issues:count", **query).get("total") or 0)


def ready(**query) -> int:
    return int(get("/ready:count", **query).get("total") or 0)


def context() -> dict:
    return get("/context")
