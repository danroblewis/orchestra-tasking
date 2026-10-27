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
    events = Events.for_project(project, source="merge_gate")      # staging deploys what the gate landed: the gate's
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
    events = Events.for_project(project, source="merge_gate")      # staging deploys what the gate landed: the gate's
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


def test_staging_that_stops_answering_is_restarted_with_backoff(project, tmp_path):
    """SPEC section 5: the scheduler restarts staging. A build alive by pid but unanswered on its
    health check for HEALTH_STALE is restarted; a restart that fails is tried again after a pause
    that doubles, the supervisor is told once, and a healthy restart ends the outage. Staging the
    operator stopped stays stopped."""
    port = free_port()
    toml = ('[staging]\nstart = "python3 -m http.server {port} --bind 127.0.0.1 --directory {artifact}"\n'
            f'port = {port}\nhealth = "/ok"\nhealth_timeout = "2s"\n')
    (project.repo_root / "orchestra.toml").write_text(toml)
    project.config = Config.from_toml(toml)
    events = Events.for_project(project, source="merge_gate")      # staging deploys what the gate landed: the gate's
    store = LocalStore(project, events)
    st = Staging(project, store, events)
    art = tmp_path / "art"
    art.mkdir()
    ok = art / "ok"
    ok.write_text("ok")
    build = {"id": "b1", "commit": "c1", "artifact_path": str(art)}
    store.record_build(build)
    staging_events = lambda type_: [e for e in events.read(0, limit=10000)[0] if e["type"] == type_]
    try:
        assert st.deploy(build)
        ok.unlink()                                                          # alive, but the health check fails
        store.set_staging(last_health_at="2000-01-01T00:00:00Z")             # for longer than HEALTH_STALE
        st.ensure_running()                                                  # stale: restarted at once, and that fails
        s = st.state()
        assert s["status"] == "restarting" and s["restart_attempts"] == 1
        assert 25 < s["restart_at"] - time.time() <= 30                      # RESTART_FIRST_S
        assert [e.get("reason", "").startswith("health check unanswered") for e in staging_events("staging.failed")] == [True, True]
        told = asked(events, "supervisor")
        assert len(told) == 1 and "could not be restarted" in told[0]["reason"]
        st.ensure_running()                                                  # not due yet: no tight loop
        assert st.state()["restart_attempts"] == 1 and len(staging_events("staging.failed")) == 2
        store.set_staging(restart_at=0)                                      # due: fails again, the pause doubles
        st.ensure_running()
        s = st.state()
        assert s["restart_attempts"] == 2 and 55 < s["restart_at"] - time.time() <= 60
        assert len(asked(events, "supervisor")) == 1                         # told once per outage
        ok.write_text("ok")                                                  # the build answers again
        store.set_staging(restart_at=0)
        st.ensure_running()
        s = st.state()
        assert s["status"] == "running" and s["alive"] and s["restart_attempts"] == 0 and s["failed_build"] is None
        assert [e["build"] for e in staging_events("staging.restarted")] == ["b1"]
        st.stop()                                                            # the operator's stop is left alone
        st.ensure_running()
        assert st.state()["status"] == "stopped" and not st.state()["alive"]
    finally:
        st.stop()
