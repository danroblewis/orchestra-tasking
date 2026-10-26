"""The Coordinator is escalated to at the threshold and its multiples, not on every rejection;
a send-back by Orchestra is not a reviewer's rejection."""

import subprocess

import pytest

from tasking.config import init_project
from tasking.events import Events
from tasking.loop import Loop
from tasking.model import State
from tasking.store import LocalStore


def asked(events, to):
    """What the task system asked the planner ``to`` to look at: its attention events on the bus."""
    return [e["data"] for e in events.bus.read(0, limit=100000)[0] if e.get("type") == "task.attention_needed" and (e.get("data") or {}).get("to") == to]



@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return init_project(repo, prefix="T")


def test_escalation_fires_at_the_threshold_and_its_multiples(project):
    store, events = LocalStore(project), Events(project.events_file)
    loop = Loop(project, store, events)
    m = store.create_milestone(name="m")
    t = store.create_ticket(title="hard", milestone_id=m.id)
    fired = []
    for i in range(1, 8):
        t = store.get_ticket(t.id)
        if t.state == State.OPEN or t.state == State.REJECTED:
            t = store.transition(t.id, t.version, State.IN_PROGRESS)
        t = store.transition(t.id, t.version, State.READY_FOR_REVIEW)
        t = store.transition(t.id, t.version, State.IN_REVIEW)
        loop._reject(t.id, "reviewer" if i % 2 else "orchestra", "no", "")
        if len(asked(events, "coordinator")) > len(fired):
            fired.append(i)
    assert fired == [3, 6]                                              # threshold 3: rounds 3 and 6, not 4, 5, 7
    t = store.get_ticket(t.id)
    assert t.review_rounds == 7 and t.reviewer_rejections == 4           # Orchestra's send-backs are not the reviewer's
