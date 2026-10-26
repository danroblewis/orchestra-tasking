"""What Orchestra prepends to a human's message to the Supervisor."""

import subprocess

import pytest

from tasking import agents
from tasking.config import init_project
from tasking.store import LocalStore


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return init_project(repo, provider="p", model="m", prefix="T")


def test_context_document_is_sent_once_until_it_changes(project):
    store = LocalStore(project)
    snap = store.snapshot()
    first = agents.supervisor_context(snap, [], project, store)
    assert first.startswith("[Orchestra: project context]") and "## Objectives" in first
    assert agents.supervisor_context(snap, [], project, store) == ""          # "yes, go ahead" arrives as itself
    agents.write_context(project, "# Project context\n\n## Objectives\n- ship it\n")
    again = agents.supervisor_context(snap, [], project, store)
    assert again.startswith("[Orchestra: project context, updated]") and "ship it" in again
    assert agents.supervisor_context(snap, [], project, store) == ""
    # events still arrive, compactly
    evs = [{"type": "ticket_transition", "ticket": "T-1", "from": "OPEN", "to": "DONE"}]
    out = agents.supervisor_context(snap, evs, project, store)
    assert "[Orchestra: project context" not in out and "ticket_transition ticket=T-1" in out
    # without a store (the CLI's temporary session) the document is always included
    assert agents.supervisor_context(snap, [], project).startswith("[Orchestra: project context]")
