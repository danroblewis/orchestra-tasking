"""A resident agent can be started over: its transcript is archived as a run and the next
process begins with an empty one."""

import subprocess

import pytest

from tasking import api
from tasking.config import init_project
from tasking.events import Events
from tasking.loop import Loop
from tasking.store import LocalStore, StoreError


class FakeProc:
    def __init__(self, sid):
        self.session_id, self._alive, self.session_file, self.harness_name = sid, True, None, "fake"
    def compact(self):
        return None
    def alive(self):
        return self._alive
    @property
    def pid(self):
        return 4242 if self._alive else None
    def close(self):
        self._alive = False


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return init_project(repo, provider="p", model="m", prefix="T")


class CompactingProc(FakeProc):
    def compact(self):
        return {"tokens_before": 150000, "tokens_after": 32000}


def test_large_sessions_are_compacted_in_place_when_the_harness_can(project):
    store, events = LocalStore(project), Events(project.events_file)
    loop = Loop(project, store, events)
    f = project.sessions_dir / "2026_worker-T-1.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x" * 10)
    store.set_session("worker-T-1", kind="worker", status="idle", session_file=str(f))
    proc = CompactingProc("worker-T-1")
    assert loop._trim_if_large(proc, "worker-T-1", "worker") is None                        # small: left alone
    f.write_text("x" * (project.config.session_max_bytes + 1))
    assert loop._trim_if_large(proc, "worker-T-1", "worker") == "compacted"
    assert f.exists() and proc.alive()                                                       # transcript and process kept
    s = store.find_session("worker-T-1")
    assert s["compactions"] == 1 and s["compacted_at_bytes"] == f.stat().st_size
    assert loop._trim_if_large(proc, "worker-T-1", "worker") is None                        # growth is measured since the compaction
    f.write_text("x" * (2 * project.config.session_max_bytes + 10))
    assert loop._trim_if_large(proc, "worker-T-1", "worker") == "compacted"
    ev = [e for e in events.read(0)[0] if e["type"] == "agent_compacted"]
    assert len(ev) == 2 and ev[0]["tokens_before"] == 150000 and ev[0]["tokens_after"] == 32000


def test_a_self_compacting_harness_is_left_alone(project):
    class AutoProc(FakeProc):
        def compact(self):
            return {"mode": "auto", "limit": 100000}
    store, events = LocalStore(project), Events(project.events_file)
    loop = Loop(project, store, events)
    f = project.sessions_dir / "2026_worker-T-3.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x" * (project.config.session_max_bytes + 1))
    store.set_session("worker-T-3", kind="worker", status="idle", session_file=str(f))
    proc = AutoProc("worker-T-3")
    assert loop._trim_if_large(proc, "worker-T-3", "worker") == "compacted" and f.exists() and proc.alive()
    ev = [e for e in events.read(0)[0] if e["type"] == "agent_compacted"][-1]
    assert ev["mode"] == "auto" and "100,000 tokens" in ev["reason"]


def test_large_sessions_rotate_only_when_the_harness_cannot_compact(project):
    store, events = LocalStore(project), Events(project.events_file)
    loop = Loop(project, store, events)
    f = project.sessions_dir / "2026_worker-T-2.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x" * (project.config.session_max_bytes + 1))
    store.set_session("worker-T-2", kind="worker", status="idle", session_file=str(f))
    assert loop._trim_if_large(FakeProc("worker-T-2"), "worker-T-2", "worker") == "rotated"   # base compact() returns None
    assert not f.exists() and (project.sessions_dir / "archive" / f.name).exists()
    assert store.find_session("worker-T-2")["session_file"] is None
    ev = [e for e in events.read(0)[0] if e["type"] == "agent_reset"][-1]
    assert ev["by"] == "orchestra" and "could not compact" in ev["reason"]


def test_reset_archives_the_transcript_and_starts_over(project):
    store, events = LocalStore(project), Events(project.events_file)
    f = project.sessions_dir / "2026_supervisor.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("old conversation\n")
    store.set_session("supervisor", kind="supervisor", status="idle", pid=4242, session_file=str(f), last_text="long ago")
    store.set_agent_state("supervisor", context_hash="abc", turn_depth=2)
    with pytest.raises(StoreError):
        api.handle(project, store, "reset_agent", {"name": "worker"}, role="supervisor")     # not resident
    api.handle(project, store, "reset_agent", {"name": "supervisor"}, role="supervisor")
    loop = Loop(project, store, events)
    loop.agent_procs["supervisor"] = proc = FakeProc("supervisor")
    loop._refresh_org()
    assert not proc.alive() and "supervisor" not in loop.agent_procs
    live = store.find_session("supervisor")
    assert live["session_file"] is None and live["last_text"] == ""                           # fresh
    archived = [k for k in store.runtime()["archived_sessions"] if k.startswith("supervisor-reset-")]
    assert len(archived) == 1
    old = store.runtime()["archived_sessions"][archived[0]]
    assert old["last_text"] == "long ago" and old["session_file"].endswith("archive/2026_supervisor.jsonl")
    assert (project.sessions_dir / "archive" / "2026_supervisor.jsonl").read_text() == "old conversation\n" and not f.exists()
    st = store.agent_state("supervisor")
    assert not st.get("reset_requested") and st.get("context_hash") is None and st.get("turn_depth") == 0   # context is re-sent
    ev = [e for e in events.read(0)[0] if e["type"] == "agent_reset"][0]
    assert ev["archived"] == archived[0] and ev["by"] == "supervisor"
    runs = api.handle(project, store, "get_agent", {"name": "supervisor"})["runs"]
    assert archived[0] in [r["id"] for r in runs]                                              # the old conversation is still a run
