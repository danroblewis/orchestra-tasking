"""The simulator's client for Beads: a few calls to ``bd serve`` (Beads' HTTP API, /v0/beads), and
the ``bd`` command for what the API does not offer (lease heartbeats, reclaiming stale leases).
Beads itself is the real ``bd`` program, run as a service (bin/beads-ctl); nothing here replaces it.

Routes, as Beads 1.3.2 serves them: GET /issues (status, type, parent, assignee, limit), GET
/ready (type), POST /issues (create), PATCH /issues/{id} (a patch, optionally conditional on
expected_status / expected_assignee), POST /issues/{id}:claim | :close | :reopen | :release,
GET /events?since=N (the durable journal), GET /stats."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

URL = os.environ.get("TASKING_BEADS_URL", "http://127.0.0.1:7811/v0/beads")
ROOT = Path(__file__).resolve().parent.parent


class BeadsError(RuntimeError):
    def __init__(self, status: int, body: dict):
        super().__init__(f"{status}: {body.get('detail') or body}")
        self.status, self.body = status, body


def _call(method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
    url = URL + path + ("?" + urllib.parse.urlencode({k: v for k, v in (query or {}).items() if v is not None}) if query else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}")
        except ValueError:
            detail = {}
        raise BeadsError(e.code, detail) from None


class Beads:
    def __init__(self, actor: str):
        self.actor = actor

    def create(self, title: str, *, issue_type: str = "task", parent: str | None = None, depends_on: list[str] = (),
               priority: int = 2, estimated_minutes: int | None = None, labels: list[str] = (), description: str = "") -> dict:
        body = {"actor": self.actor, "title": title, "issue_type": issue_type, "priority": priority, "description": description}
        if parent:
            body["parent_id"] = parent
        if estimated_minutes:
            body["estimated_minutes"] = estimated_minutes
        if labels:
            body["labels"] = list(labels)
        if depends_on:
            body["dependencies"] = [{"target_id": d, "type": "blocks"} for d in depends_on]
        return _call("POST", "/issues", body)

    def list(self, **query) -> list[dict]:
        return _call("GET", "/issues", query={"limit": 500, **query}).get("items", [])

    def ready(self, issue_type: str = "task") -> list[dict]:
        return _call("GET", "/ready", query={"type": issue_type}).get("items", [])

    def claim(self, issue_id: str) -> dict | None:
        """The issue, now ours; None when someone else holds it."""
        try:
            r = _call("POST", f"/issues/{issue_id}:claim", {"actor": self.actor})
        except BeadsError as e:
            if e.status == 409:                                  # another worker got there first
                return None
            raise
        issue = r.get("issue") or {}
        return issue if issue.get("assignee") == self.actor and not r.get("already_claimed") else None

    def update(self, issue_id: str, patch: dict, **expected) -> dict:
        return _call("PATCH", f"/issues/{issue_id}", {"actor": self.actor, "patch": patch, **{f"expected_{k}": v for k, v in expected.items()}})

    def close(self, issue_id: str, reason: str) -> dict:
        return _call("POST", f"/issues/{issue_id}:close", {"actor": self.actor, "reason": reason})

    def reopen(self, issue_id: str, reason: str) -> dict:
        return _call("POST", f"/issues/{issue_id}:reopen", {"actor": self.actor, "reason": reason})

    def comment(self, issue_id: str, text: str) -> dict:
        return _call("POST", f"/issues/{issue_id}/comments", {"actor": self.actor, "text": text})

    def stats(self) -> dict:
        return _call("GET", "/stats")

    # the command line, for what the API does not serve
    def bd(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        env = {**os.environ, "BD_NON_INTERACTIVE": "1", "BD_ACTOR": self.actor}
        return subprocess.run(["bd", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)

    def heartbeat(self, issue_id: str) -> bool:
        return self.bd("heartbeat", issue_id).returncode == 0
