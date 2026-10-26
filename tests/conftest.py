"""What the tests share: a project in a fresh git repository under a temporary home, a free port,
and what the task system asked a planner to look at."""

import socket
import subprocess

import pytest

from tasking.config import init_project

UNIT = "test -f README.md && test ! -f BROKEN"     # the starter unit command: passes unless a worker breaks the tree


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A repository with one committed README and the task system's starter config (prefix T,
    work = tasks, the UNIT command), its state under a temporary ORCHESTRATOR_HOME, named by
    ORCHESTRA_REPO so that `load_project()` finds it."""
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "README.md").write_text("# test\nline one\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    p = init_project(repo, prefix="T", unit_command=UNIT)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "orchestra config", cwd=repo)
    monkeypatch.setenv("ORCHESTRA_REPO", str(repo))
    return p


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def asked(events, to):
    """What the task system asked the planner ``to`` to look at: its attention events on the bus."""
    return [e["data"] for e in events.bus.read(0, limit=100000)[0]
            if e.get("type") == "task.attention_needed" and (e.get("data") or {}).get("to") == to]
