"""What the tests share: a project in a fresh git repository under a temporary home, a free port,
the scheduler run once with a fake machine, what is on the bus, and waiting for something."""

import socket
import subprocess
import time

import pytest
from fake_machine import FakeMachine

from tasking.assignments import (
    ACCEPTED,
    APPROVED,
    CHANGES_REQUESTED,
    OFFERS,
    REVIEW_REQUESTED,
    pass_on,
)
from tasking.config import init_project
from tasking.events import Events
from tasking.scheduler import Scheduler
from tasking.store import LocalStore

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


@pytest.fixture
def owner_layout(tmp_path, monkeypatch):
    """The layout of an owner with a large repository: one bare clone ``Code/bare/.git`` with
    worktrees ``Code/worktree_1..4`` beside it (worktree_1 on main, the others on branches
    ``mine-<n>``), the project in worktree_1, in code mode, with ``tasks`` added to its ``[tasks]``,
    ``files`` committed upstream and ``unit`` as the gate's unit command. ``HOME`` is the
    temporary directory, so ``~/Code/...`` names the worktrees. Returns (project, store, events, Code)."""
    def make(tasks: str = "", files: dict[str, str] | None = None, unit: str = UNIT):
        monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
        monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
        src = tmp_path / "src"
        src.mkdir()
        git("init", "-q", "-b", "main", cwd=src)
        for name, text in {"README.md": "# monorepo\n", ".gitignore": "cache/\n", **(files or {})}.items():
            (src / name).parent.mkdir(parents=True, exist_ok=True)
            (src / name).write_text(text)
        git("add", "-A", cwd=src)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=src)
        code = tmp_path / "Code"
        bare = code / "bare" / ".git"
        git("clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
        git("config", "user.email", "t@t", cwd=bare)
        git("config", "user.name", "t", cwd=bare)
        git("worktree", "add", "-q", "../../worktree_1", "main", cwd=bare)
        for i in (2, 3, 4):
            git("worktree", "add", "-q", "-b", f"mine-{i}", f"../../worktree_{i}", "main", cwd=bare)
        wt1 = code / "worktree_1"
        project = init_project(wt1, prefix="T", unit_command=unit)
        toml = wt1 / "orchestra.toml"
        text = toml.read_text().replace('work = "tasks"', 'work = "code"')
        toml.write_text(text.replace("[tasks]\n", f"[tasks]\n{tasks}\n"))
        git("add", "-A", cwd=wt1)
        git("commit", "-qm", "orchestra config", cwd=wt1)
        monkeypatch.setenv("ORCHESTRA_REPO", str(wt1))
        monkeypatch.setenv("HOME", str(tmp_path))
        project.reload_config()
        events = Events.for_project(project, source="task_scheduler")
        return project, LocalStore(project, events), events, code
    return make


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def asked(events, to):
    """What the task system asked the planner ``to`` to look at: its attention events on the bus."""
    return [e["data"] for e in events.bus.read(0, limit=100000)[0]
            if e.get("type") == "task.attention_needed" and (e.get("data") or {}).get("to") == to]


def on_bus(events, type_, **match):
    """The events of ``type_`` on the bus whose data has every ``match`` field, in order."""
    return [e for e in events.bus.read(0, limit=10000)[0]
            if e["type"] == type_ and all((e["data"] or {}).get(k) == v for k, v in match.items())]


def moves(store) -> dict[tuple[str, str, str], int]:
    """The record's transition counters (tasking/measures.py): {(from, to, by): count}."""
    from tasking.measures import TRANSITIONS, counters
    return {(d["from"], d["to"], d["by"]): n for (name, labels), n in counters(store._t()["metric_counts"]).items()
            if name == TRANSITIONS for d in [dict(labels)]}


def run_scheduler(project, store, events, **behaviour):
    """The scheduler until nothing is left to do, with a fake machine (tests/fake_machine.py)
    filling the roles as ``behaviour`` says; the machine, for what it saw."""
    with FakeMachine(events, **behaviour) as m:
        Scheduler(project, store, events).run(once=True)
    return m


def wait_for(check, timeout_s=5.0):
    end = time.time() + timeout_s
    while time.time() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("did not happen in time")


def standing(events, store, role: str) -> dict:
    """The offer for ``role`` that stands in the record, as the bus carried it: whichever event
    offered it (the queue's ``task.assigned``, a worker's ``review.requested``, a reviewer's
    ``changes.requested``)."""
    held = store.assignments()
    return [e for e in events.after(0)[0] if e["type"] in OFFERS and e.get("role") == role and e.get("assignment") in held][-1]


def claim(events, offer: dict, source: str | None = None) -> dict:
    """``source`` (default ``<role>/1``) claims ``offer``."""
    source = source or f"{offer['role']}/1"
    return events.publish(ACCEPTED, {"assignment": offer["assignment"], "ticket": offer["ticket"], "role": offer["role"]},
                          source=source, subject=offer["ticket"])


def end(events, offer: dict, kind: str, source: str | None = None, **fields) -> dict:
    """The holder ``source`` (default ``<role>/1``) ends the turn ``offer`` opened: a review
    requested, changes requested, an approval, or (any other ``kind``) that event with the
    assignment and ``fields``."""
    source = source or f"{offer['role']}/1"
    tid = offer["ticket"]
    if kind == REVIEW_REQUESTED:
        data = pass_on(offer, kind, source=source, review={"summary": fields.pop("summary", "done"), "commit": fields.pop("commit", None),
                                                           "test_output": None, "worker": source}, **fields)
    elif kind == CHANGES_REQUESTED:
        data = pass_on(offer, kind, source=source, summary="no", feedback=fields.pop("feedback", "no"), **fields)
    elif kind == APPROVED:
        data = {"ends": offer["assignment"], "ticket": tid, "role": offer["role"], "summary": "yes", **fields}
    else:
        data = {"assignment": offer["assignment"], "ticket": tid, "role": offer["role"], **fields}
    return events.publish(kind, data, source=source, subject=tid)


def handoffs(events, subject: str) -> list[tuple[str, str | None, str]]:
    """The hand-offs Orchestra's Machines page and event map compute from the bus (its
    ``MachineIndex``, behind ``/api/machines``), in the order they arose, for the events about
    ``subject``: (from machine, via event type, to machine). The index reads the bus as it grew."""
    from orchestra.web.machines import MachineIndex

    seen: list[dict] = []

    class Grown:                                     # the bus as it stood after each event
        def end(self):
            return len(seen)

        def read(self, since=0, limit=500):
            return seen[since:][-limit:], len(seen)

    idx, out = MachineIndex(Grown()), []
    for ev in events.bus.read(0, limit=100000)[0]:
        before = dict(idx.handoffs)
        seen.append(ev)
        idx.update()
        if ev.get("subject") == subject:
            out += [k for k, n in idx.handoffs.items() if n > before.get(k, 0)]
    return out
