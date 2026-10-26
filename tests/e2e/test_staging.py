"""Staging supervisor against a trivial HTTP server as the 'artifact'."""

import subprocess
import time
import urllib.request

import pytest

from tasking.config import Config, init_project
from tasking.events import Events
from tasking.staging import Staging, record_build
from tasking.store import LocalStore


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    p = init_project(repo)
    (repo / "orchestra.toml").write_text(
        '[staging]\nstart = "python3 -m http.server {port} --bind 127.0.0.1 --directory {artifact}"\n'
        'port = 18765\nhealth = "/"\nhealth_timeout = "20s"\n')
    p.config = Config.from_toml((repo / "orchestra.toml").read_text())
    return p


def test_deploy_restart_rollback(project, tmp_path):
    store = LocalStore(project)
    st = Staging(project, store, Events(project.events_file))
    good = tmp_path / "good"
    good.mkdir()
    (good / "index.html").write_text("v1")
    build1 = {"id": "b1", "commit": "c1", "artifact_path": str(good)}
    record_build(store, build1)
    try:
        assert st.deploy(build1)
        assert urllib.request.urlopen("http://127.0.0.1:18765/").read() == b"v1"
        assert st.state()["status"] == "running" and st.state()["alive"]
        # A bad build fails health and rolls back to b1.
        bad = {"id": "b2", "commit": "c2", "artifact_path": str(tmp_path / "missing")}
        record_build(store, bad)
        assert not st.deploy(bad)
        time.sleep(0.5)
        assert st.state()["current_build_id"] == "b1"
        assert urllib.request.urlopen("http://127.0.0.1:18765/").read() == b"v1"
        wake = store.drain_wakeups("supervisor")
        assert wake and "staging deploy" in wake[0]["reason"]
        # Supervision restarts a dead process.
        import os, signal
        os.killpg(st.state()["pid"], signal.SIGKILL)
        time.sleep(0.5)
        st.ensure_running()
        assert st.state()["alive"]
    finally:
        st.stop()
    assert not st.state()["alive"]
