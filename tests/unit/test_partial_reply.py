"""While a reply streams, the session record carries the text so far, so the UI can show
what an agent is typing during a long generation."""

import subprocess

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


def test_partial_reply_is_recorded_and_cleared(project):
    store, events = LocalStore(project), Events(project.events_file)
    loop = Loop(project, store, events)
    store.set_session("supervisor", kind="supervisor", status="running")
    fn = loop._on_event("supervisor")
    fn({"type": "assistant_start"})
    fn({"type": "token", "kind": "thinking", "delta": "let me see"})
    fn({"type": "token", "kind": "text", "delta": "I'll create "})
    loop._last_token_write["supervisor"] = 0                    # force the periodic write
    fn({"type": "token", "kind": "text", "delta": "the ticket now."})
    s = store.find_session("supervisor")
    assert s["partial_text"] == "I'll create the ticket now." and s["thinking_chars"] == len("let me see")
    fn({"type": "assistant_end", "text": "I'll create the ticket now."})
    s = store.find_session("supervisor")
    assert s["partial_text"] == "" and s["thinking_chars"] == 0 and s["last_text"].endswith("now.")
