"""Staging against a trivial HTTP server as the 'artifact'."""

import os
import signal
import socket
import time
import urllib.request

import pytest
from conftest import asked, free_port

from tasking.config import Config
from tasking.events import Events
from tasking.staging import Staging
from tasking.store import LocalStore


@pytest.fixture
def staged(project):
    port = free_port()
    (project.repo_root / "orchestra.toml").write_text(
        '[staging]\nstart = "python3 -m http.server {port} --bind 127.0.0.1 --directory {artifact}"\n'
        f'port = {port}\nhealth = "/"\nhealth_timeout = "20s"\n')
    project.config = Config.from_toml((project.repo_root / "orchestra.toml").read_text())
    return project, port


def test_deploy_restart_rollback(staged, tmp_path):
    project, port = staged
    url = f"http://127.0.0.1:{port}/"
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    st = Staging(project, store, events)
    good = tmp_path / "good"
    good.mkdir()
    (good / "index.html").write_text("v1")
    build1 = {"id": "b1", "commit": "c1", "artifact_path": str(good)}
    store.record_build(build1)
    try:
        assert st.deploy(build1)
        assert urllib.request.urlopen(url).read() == b"v1"
        assert st.state()["status"] == "running" and st.state()["alive"]
        # A bad build fails health and rolls back to b1.
        bad = {"id": "b2", "commit": "c2", "artifact_path": str(tmp_path / "missing")}
        store.record_build(bad)
        assert not st.deploy(bad)
        time.sleep(0.5)
        assert st.state()["current_build_id"] == "b1"
        assert urllib.request.urlopen(url).read() == b"v1"
        wake = asked(events, "supervisor")
        assert wake and "staging deploy" in wake[0]["reason"]
        # Supervision restarts a dead process.
        os.killpg(st.state()["pid"], signal.SIGKILL)
        time.sleep(0.5)
        st.ensure_running()
        assert st.state()["alive"]
    finally:
        st.stop()
    assert not st.state()["alive"]


def test_a_port_held_by_another_process_is_not_taken(staged, tmp_path):
    """The task system kills only what it started: a deploy onto a port someone else holds
    fails and says so, and the other process is left alone."""
    project, port = staged
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    st = Staging(project, store, events)
    (tmp_path / "good").mkdir()
    build = {"id": "b1", "commit": "c1", "artifact_path": str(tmp_path / "good")}
    store.record_build(build)
    with socket.socket() as other:                       # another process, as far as staging is concerned
        other.bind(("127.0.0.1", port))
        other.listen()
        assert st.deploy(build) is False
        state = st.state()
        assert state["status"] == "failed" and state["reason"] == f"port {port} in use by another process" and state["pid"] is None
        assert f"port {port} in use by another process" in st.logs()
        assert [e["type"] for e in events.read(0, limit=1000)[0] if e["type"].startswith("staging.")] == ["staging.failed"]
        other.getsockname()                              # still ours and still open
    assert not st.state()["alive"]
