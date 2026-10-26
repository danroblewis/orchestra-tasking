"""Resident agents are supervised: a dead process is never shown as alive, is restarted,
and a crash loop pauses the agent and wakes the Supervisor."""

import subprocess
import time

import pytest

from tasking.config import init_project
from tasking.events import Events
from tasking.loop import Loop
from tasking.store import LocalStore


class FakeProc:
    def __init__(self, sid, alive=True, pid=4242):
        self.session_id, self._alive, self._pid, self.session_file, self.harness_name = sid, alive, pid, None, "fake"
    def alive(self):
        return self._alive
    @property
    def pid(self):
        return self._pid if self._alive else None
    def close(self):
        self._alive = False


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return init_project(repo, provider="p", model="m", prefix="T")


def test_dead_process_has_no_pid(project):
    assert Loop._pid(FakeProc("x", alive=True)) == 4242
    assert Loop._pid(FakeProc("x", alive=False)) is None            # never the loop's own pid for a dead harness


def test_crashed_resident_is_recorded_restarted_and_crash_looped(project):
    store, events = LocalStore(project), Events(project.events_file)
    loop = Loop(project, store, events)
    (project.logs_dir / "agents").mkdir(parents=True, exist_ok=True)
    (project.logs_dir / "agents" / "retro.stderr.log").write_text("boom: model not found\n")
    for n in (1, 2, 3):
        store.set_session("retro", kind="retro", status="idle", pid=4242)
        loop.agent_procs["retro"] = FakeProc("retro", alive=False)
        loop._reap_residents()
        assert "retro" not in loop.agent_procs                        # dropped: the next tick starts it again
        assert store.find_session("retro")["pid"] is None
        crashes = [e for e in events.read(0)[0] if e["type"] == "agent_crashed"]
        assert len(crashes) == n and crashes[-1]["count"] == n and "model not found" in crashes[-1]["stderr"]
    st = store.agent_state("retro")
    assert st["paused"] and "3 crashes" in st["crash_loop"]
    assert [e["type"] for e in events.read(0)[0]][-1] == "agent_crash_loop"
    wake = store.drain_wakeups("supervisor")
    assert wake and "crashed 3 times" in wake[0]["reason"] and "model not found" in wake[0]["reason"]
    # an agent mid-turn is left to its turn; a live one is left alone
    loop.agent_procs["busy"] = FakeProc("busy", alive=False)
    loop.agent_threads["busy"] = type("T", (), {"is_alive": lambda self: True})()
    loop.agent_procs["fine"] = FakeProc("fine", alive=True)
    loop._reap_residents()
    assert "busy" in loop.agent_procs and "fine" in loop.agent_procs
    # old crashes fall out of the window
    store.set_agent_state("slow", crashes=[time.time() - 3600] * 5)
    store.set_session("slow", kind="slow", status="idle", pid=1)
    loop.agent_procs["slow"] = FakeProc("slow", alive=False)
    loop._reap_residents()
    assert not store.agent_state("slow").get("paused")
