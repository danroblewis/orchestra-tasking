"""A turn Orchestra cut off is an event with what the agent was doing, so the transcript can say so."""

import subprocess
import time
from types import SimpleNamespace

import pytest

from tasking.config import init_project
from tasking.events import Events
from tasking.loop import Loop
from tasking.store import LocalStore


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return init_project(repo, provider="p", model="m", prefix="T")


def test_timed_out_turn_is_recorded_with_what_the_agent_was_doing(project):
    store, events = LocalStore(project), Events(project.events_file)
    loop = Loop(project, store, events)
    now_t = time.time()
    store.set_session("retro-1", kind="retrospective", status="running", phase="tool", tool_name="bash",
                      phase_since=now_t - 130, last_output_at=now_t - 12, started_at=now_t - 900)
    loop._note_turn("retro-1", SimpleNamespace(timed_out=True, error=None, text="Let me read the transcript."), "retrospective")
    loop._note_turn("retro-1", SimpleNamespace(timed_out=False, error=None, text="fine"), "retrospective")   # a normal end: nothing
    loop._note_turn("retro-2", SimpleNamespace(timed_out=False, error="pi exited 1", text=""), "retrospective")
    evs = [e for e in events.read(0)[0] if e["type"].startswith("turn_")]
    assert [e["type"] for e in evs] == ["turn_timed_out", "turn_error"]
    t = evs[0]
    assert t["session"] == "retro-1" and t["agent"] == "retrospective" and t["limit"] == project.config.turn_timeout
    assert t["doing"] == "running `bash`" and 125 <= t["phase_for"] <= 135 and 10 <= t["silent_for"] <= 15
    assert t["last_text"] == "Let me read the transcript."
    assert evs[1]["error"] == "pi exited 1"
    loop.stop.set()                                                                      # a harness exit during shutdown is not an error
    loop._note_turn("retro-3", SimpleNamespace(timed_out=False, error="pi exited", text=""), "retrospective")
    last = events.read(0)[0][-1]
    assert last["type"] == "turn_interrupted" and last["session"] == "retro-3"


def test_shutdown_marks_every_running_turn(project):
    store, events = LocalStore(project), Events(project.events_file)
    store.set_session("worker-T-1", kind="worker", status="running", ticket_id="T-1")
    store.set_session("po", kind="po", status="idle")
    loop = Loop(project, store, events)
    loop.stop.set()
    loop.run(once=True)
    cut = [e for e in events.read(0)[0] if e["type"] == "turn_interrupted"]
    assert [(e["session"], e["ticket"]) for e in cut] == [("worker-T-1", "T-1")]     # found running at startup: a crash
    assert "crashed" in cut[0]["reason"]
    store.set_session("po", status="running")
    loop._shutdown_markers()
    cut = [e for e in events.read(0)[0] if e["type"] == "turn_interrupted"]
    assert cut[-1]["session"] == "po" and "shut down" in cut[-1]["reason"]           # cut off by a clean stop
