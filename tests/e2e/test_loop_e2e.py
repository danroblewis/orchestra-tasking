"""End-to-end: the real loop, store, git, and gate driven by a fake harness.

Every test runs once per harness: the fake Pi (RPC protocol, tools via the extension's
`tasking api` path), the fake Claude Code (stream-json, tools via the real MCP server),
and the fake Codex (`exec --json`, tools via the same MCP server). The loop must behave
identically on all of them."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tasking.config import init_project
from tasking.events import Events
from tasking.loop import Loop
from tasking.model import State
from tasking.store import LocalStore

FAKES = {"pi": Path(__file__).parent / "fake_pi.py", "claude_code": Path(__file__).parent / "fake_claude.py",
         "codex": Path(__file__).parent / "fake_codex.py"}
BINARY_ENV = {"pi": "PI_BINARY", "claude_code": "CLAUDE_BINARY", "codex": "CODEX_BINARY"}


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture(params=["pi", "claude_code", "codex"])
def env(tmp_path, monkeypatch, request):
    harness = request.param
    home = tmp_path / "home"
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(home))
    fake = tmp_path / harness
    shutil.copy(FAKES[harness], fake)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(BINARY_ENV[harness], str(fake))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("FAKE_PI_WORKER_BLOCK", raising=False)
    monkeypatch.delenv("FAKE_PI_REJECT_FIRST", raising=False)
    monkeypatch.delenv("FAKE_PI_BREAK_FIRST", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "README.md").write_text("# e2e\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    project = init_project(repo, prefix="E2E", unit_command="test -f README.md")
    toml = repo / "orchestra.toml"
    toml.write_text(toml.read_text().replace('harness = "pi"', f'harness = "{harness}"').replace('work = "tasks"', 'work = "code"'))   # the code path: a branch per ticket, the gate
    project.reload_config()
    assert project.config.harness == harness
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "orchestra config", cwd=repo)
    monkeypatch.setenv("ORCHESTRA_REPO", str(repo))
    events = Events(project.events_file)
    store = LocalStore(project, events)
    ms = store.create_milestone(name="m")
    return project, store, events, ms, repo


def run_loop(project, store, events):
    Loop(project, store, events).run(once=True)


def test_ticket_goes_open_to_done(env):
    project, store, events, ms, repo = env
    t = store.create_ticket(title="Do the thing", milestone_id=ms.id, description="x", acceptance="y")
    run_loop(project, store, events)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE
    assert t.worker_turns == 1 and t.reviewer_rejections == 0 and t.gate_rejections == 0
    assert "WORK.md" in git("ls-tree", "--name-only", "main", cwd=repo)
    assert "Merge E2E-1" in git("log", "-1", "--format=%s", "main", cwd=repo)
    assert not (project.worktrees_dir / t.id).exists()
    assert not git("branch", "--list", "orchestra/*", cwd=repo).strip()
    assert {s["id"] for s in store.sessions()} == {"supervisor", "coordinator"}
    archived = store.runtime()["archived_sessions"]
    assert set(archived) == {f"worker-{t.id}", f"reviewer-{t.id}"}
    assert all(Path(a["session_file"]).exists() for a in archived.values())
    assert {a["harness"] for a in archived.values()} == {project.config.harness}
    assert store.gate_runs()[-1]["status"] == "landed"
    assert store.get_milestone(ms.id).status == "open"  # fake coordinator does not close it


def test_rejection_loops_back_to_worker(env, monkeypatch):
    project, store, events, ms, repo = env
    monkeypatch.setenv("FAKE_PI_REJECT_FIRST", "1")
    t = store.create_ticket(title="Needs two tries", milestone_id=ms.id)
    run_loop(project, store, events)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE and t.review_rounds == 1
    assert t.worker_turns == 2 and t.reviewer_rejections == 1 and t.gate_rejections == 0   # re-engaged once, by the reviewer
    kinds = [n["kind"] for n in store.notes(t.id)]
    assert kinds.count("review") == 2


def test_blocked_worker(env, monkeypatch):
    project, store, events, ms, repo = env
    monkeypatch.setenv("FAKE_PI_WORKER_BLOCK", "1")
    t = store.create_ticket(title="Blocked one", milestone_id=ms.id)
    run_loop(project, store, events)
    t = store.get_ticket(t.id)
    assert t.state == State.BLOCKED and t.blocked_reason == "need a decision"
    assert (project.worktrees_dir / t.id).exists()  # kept for resumption


def test_dependency_order(env):
    project, store, events, ms, repo = env
    a = store.create_ticket(title="A", milestone_id=ms.id, priority=5)
    b = store.create_ticket(title="B", milestone_id=ms.id, priority=1, depends_on=[a.id])
    run_loop(project, store, events)
    assert store.get_ticket(a.id).state == State.DONE
    assert store.get_ticket(b.id).state == State.DONE
    log = git("log", "--format=%s", "main", cwd=repo).splitlines()
    assert log.index(f"Merge {b.id}: B") < log.index(f"Merge {a.id}: A")  # B landed after A


def test_gate_rejection_reengages_worker_then_lands(env, monkeypatch):
    """The whole failed-merge flow: the worker's first attempt passes review but fails the
    gate's tests; the gate sends it back with feedback; the same worker session is
    re-engaged in the same worktree, fixes it, is re-reviewed, and lands."""
    project, store, events, ms, repo = env
    monkeypatch.setenv("FAKE_PI_BREAK_FIRST", "1")
    t = store.create_ticket(title="Breaks the build first", milestone_id=ms.id)
    run_loop(project, store, events)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE
    assert t.review_rounds == 1                       # the gate rejection counted as one round
    assert t.worker_turns == 2 and t.gate_rejections == 1 and t.reviewer_rejections == 0   # re-engaged once, by the gate
    runs = store.gate_runs()
    assert [r["status"] for r in runs] == ["rejected", "landed"]
    assert runs[0]["rejected"] == [t.id] and runs[1]["merged"] == [t.id]
    kinds = [(n["author"], n["kind"]) for n in store.notes(t.id)]
    assert kinds.count(("merge_gate", "gate")) == 2   # the rejection and the landing
    assert kinds.count(("reviewer", "review")) == 2   # reviewed before each gate run
    evs, _ = events.read(0, limit=10000)
    trail = [(e["from"], e["to"]) for e in evs if e["type"] == "ticket_transition" and e["ticket"] == t.id]
    assert trail == [("OPEN", "IN_PROGRESS"), ("IN_PROGRESS", "READY_FOR_REVIEW"), ("READY_FOR_REVIEW", "IN_REVIEW"),
                     ("IN_REVIEW", "APPROVED"), ("APPROVED", "MERGING"), ("MERGING", "REJECTED"),
                     ("REJECTED", "IN_PROGRESS"), ("IN_PROGRESS", "READY_FOR_REVIEW"), ("READY_FOR_REVIEW", "IN_REVIEW"),
                     ("IN_REVIEW", "APPROVED"), ("APPROVED", "MERGING"), ("MERGING", "DONE")]
    assert "README.md" in git("ls-tree", "--name-only", "main", cwd=repo)
    assert "WORK.md" in git("ls-tree", "--name-only", "main", cwd=repo)
    # One worker session for the ticket, re-engaged rather than replaced.
    archived = store.runtime()["archived_sessions"]
    assert set(archived) == {f"worker-{t.id}", f"reviewer-{t.id}"}
    from tasking.harness import read_transcript
    msgs = read_transcript(archived[f"worker-{t.id}"])
    assert sum(1 for m in msgs if m["role"] == "user") == 2
    assert any(m["role"] == "assistant" and any(c["name"] == "report_ticket" for c in m["tool_calls"]) for m in msgs)


def test_team_lead_is_woken_in_its_space(env):
    """A custom team (Stage 2): the lead is a scheduled agent that also wakes on landings.
    When a ticket lands, the loop wakes it in the team's space with a prompt made of its
    role, the team's responsibilities, and its access."""
    from tasking import api
    project, store, events, ms, repo = env
    api.handle(project, store, "create_role", {"name": "owner", "description": "Owns the backlog",
                                               "prompt": "# You own the backlog\n\n" + "Keep it full and honest. " * 4, "tools": ["tickets"]}, role="supervisor")
    api.handle(project, store, "create_agent", {"name": "po", "roles": ["owner"], "team": "product", "kind": "scheduled", "every": "1d",
                                                "wake_on": ["ticket_transition:DONE"]}, role="supervisor")
    api.handle(project, store, "create_team", {"name": "product", "lead": "po", "responsibilities": "# Product\n\n1. The backlog is never empty.",
                                               "access": [{"tools": ["events"]}]}, role="supervisor")
    store.drain_wakeups("po")   # the creation introduction; this test is about the landing
    t = store.create_ticket(title="Do the thing", milestone_id=ms.id)
    run_loop(project, store, events)
    assert store.get_ticket(t.id).state == State.DONE
    from tasking.harness import read_transcript
    sess = next(s for s in store.sessions() if s["id"] == "po")
    texts = [m["text"] for m in read_transcript(sess) if m["role"] == "user"]
    assert any("event ticket_transition" in x and "to=DONE" in x for x in texts)
    assert (project.repo_root / ".orchestra" / "spaces" / "product" / "tools").is_dir()
    assert "The backlog is never empty" in (project.logs_dir / "agents" / "po.prompt.md").read_text()   # what it was started with
    assert store.agent_state("po").get("last_woken")
