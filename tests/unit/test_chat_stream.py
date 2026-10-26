"""The Supervisor chat stream stays alive through proxies: pings go out while nothing
arrives, and the client's lines are passed through unchanged."""

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from fastapi import FastAPI

from tasking import web
from tasking.config import init_project
from tasking.store import LocalStore


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return init_project(repo, provider="p", model="m", prefix="T")


class SlowControl(BaseHTTPRequestHandler):
    """A control endpoint whose Supervisor thinks for a while before saying anything."""
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        time.sleep(0.7)
        self.wfile.write(b'{"type": "assistant_end", "text": "done thinking"}\n')
        self.wfile.write(b'{"type": "done", "error": null, "timed_out": false}\n')
        self.wfile.flush()
    def log_message(self, *a):
        pass


def test_chat_stream_pings_while_the_supervisor_is_silent(project, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), SlowControl)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    store = LocalStore(project)
    store._mutate_runtime(lambda d: d.__setitem__("control", {"port": srv.server_port, "pid": 1}))
    monkeypatch.setattr(web, "CHAT_PING_SECONDS", 0.2)
    app = FastAPI()
    app.include_router(web.router(project), prefix="/api/plugins/tasks")
    client = TestClient(app)
    with client.stream("POST", "/api/plugins/tasks/supervisor", json={"message": "hello"}) as r:
        assert r.status_code == 200
        lines = [json.loads(ln) for ln in r.iter_lines() if ln.strip()]
    kinds = [ln["type"] for ln in lines]
    assert kinds.count("ping") >= 2                                   # silence was bridged
    assert kinds[-2:] == ["assistant_end", "done"]                    # and the real lines came through in order
    srv.shutdown()
