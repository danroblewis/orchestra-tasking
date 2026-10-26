"""Staging against a trivial HTTP server as the 'artifact'."""

import time
import urllib.request

import pytest
from conftest import asked

from tasking.config import Config
from tasking.events import Events
from tasking.staging import Staging
from tasking.store import LocalStore


@pytest.fixture
def staged(project):
    (project.repo_root / "orchestra.toml").write_text(
        '[staging]\nstart = "python3 -m http.server {port} --bind 127.0.0.1 --directory {artifact}"\n'
        'port = 18765\nhealth = "/"\nhealth_timeout = "20s"\n')
    project.config = Config.from_toml((project.repo_root / "orchestra.toml").read_text())
    return project


def test_deploy_restart_rollback(staged, tmp_path):
    project = staged
    events = Events(project.events_file, source="task_scheduler")
    store = LocalStore(project, events)
    st = Staging(project, store, events)
    good = tmp_path / "good"
    good.mkdir()
    (good / "index.html").write_text("v1")
    build1 = {"id": "b1", "commit": "c1", "artifact_path": str(good)}
    store.record_build(build1)
    try:
        assert st.deploy(build1)
        assert urllib.request.urlopen("http://127.0.0.1:18765/").read() == b"v1"
        assert st.state()["status"] == "running" and st.state()["alive"]
        # A bad build fails health and rolls back to b1.
        bad = {"id": "b2", "commit": "c2", "artifact_path": str(tmp_path / "missing")}
        store.record_build(bad)
        assert not st.deploy(bad)
        time.sleep(0.5)
        assert st.state()["current_build_id"] == "b1"
        assert urllib.request.urlopen("http://127.0.0.1:18765/").read() == b"v1"
        wake = asked(events, "supervisor")
        assert wake and "staging deploy" in wake[0]["reason"]
        # Supervision restarts a dead process.
        import os
        import signal
        os.killpg(st.state()["pid"], signal.SIGKILL)
        time.sleep(0.5)
        st.ensure_running()
        assert st.state()["alive"]
    finally:
        st.stop()
    assert not st.state()["alive"]
