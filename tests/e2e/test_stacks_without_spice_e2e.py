"""``[gate] stacking = "git-spice"`` with git-spice not on PATH (tasking/stacks.py, "Without
git-spice"): the real scheduler, store and git, with git-spice taken away (``Spice.available``
false, and any call of it a failure). A ticket with no unlanded dependency gets its branch from the
base as with stacking off; one that would stack waits for its dependency to land, noted on it; the
absence is said once in the log and once as attention to the supervisor. Installed again, stacking
starts without a restart."""

import shutil
from pathlib import Path

import pytest
from conftest import asked, git

from tasking.assignments import ACCEPTED, OFFERS, REVIEW_REQUESTED, pass_on
from tasking.events import Events
from tasking.model import State
from tasking.scheduler import Scheduler
from tasking.spice import Spice
from tasking.stacks import MISSING
from tasking.store import LocalStore


@pytest.fixture
def no_spice(monkeypatch):
    def called(self, *args, **kw):
        raise AssertionError(f"git-spice was asked {args} while it is not on PATH")
    available, run = Spice.available, Spice.run
    monkeypatch.setattr(Spice, "available", lambda self: False)
    monkeypatch.setattr(Spice, "run", called)

    def install():
        monkeypatch.setattr(Spice, "available", available)
        monkeypatch.setattr(Spice, "run", run)
    return install


def setup(project):
    repo = project.repo_root
    toml = repo / "orchestra.toml"
    toml.write_text(toml.read_text().replace('work = "tasks"', 'work = "code"').replace("[gate]\n", '[gate]\nstacking = "git-spice"\n'))
    project.reload_config()
    assert project.config.stacked
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "stacking", cwd=repo)
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    sched = Scheduler(project, store, events)
    sched.recover()
    sched.announce()
    return store, events, sched, store.create_milestone(src="planner", name="m")


def said(project) -> str:
    """The scheduler's log, as the owner reads it."""
    return (project.logs_dir / "scheduler.log").read_text()


def tick(sched, n=1):
    for _ in range(n):
        sched.tick()


def offer(events, store, tid, role):
    held = {a["id"] for a in store.assignments().values() if a["ticket"] == tid and a["role"] == role and a.get("instance") is None}
    found = [e for e in events.after(0)[0] if e["type"] in OFFERS and e.get("assignment") in held]
    return found[-1] if found else None


def ask_review(sched, events, o, who="worker/1"):
    events.publish(ACCEPTED, {"assignment": o["assignment"], "ticket": o["ticket"], "role": "worker"}, source=who, subject=o["ticket"])
    tick(sched)
    Path(o["workspace"], "a.txt").write_text("a\n")
    git("add", "-A", cwd=o["workspace"])
    git("commit", "-qm", "a", cwd=o["workspace"])
    head = git("rev-parse", "HEAD", cwd=o["workspace"]).strip()
    events.publish(REVIEW_REQUESTED, pass_on(o, REVIEW_REQUESTED, source=who, review={"summary": "done", "commit": head,
                                                                                     "test_output": None, "worker": who}),
                   source=who, subject=o["ticket"])
    tick(sched)


def test_without_git_spice_an_unstacked_ticket_still_starts_and_a_stacked_one_waits(project, no_spice):
    store, events, sched, ms = setup(project)
    a = store.create_ticket(src="planner", title="a", milestone_id=ms.id)
    b = store.create_ticket(src="planner", title="b", milestone_id=ms.id, depends_on=[a.id])
    tick(sched, 3)
    oa = offer(events, store, a.id, "worker")
    assert oa is not None and oa["base"] == "main"                        # a plain branch from the base, git-spice never asked
    assert git("rev-parse", oa["branch"], cwd=project.repo_root) == git("rev-parse", "main", cwd=project.repo_root)

    ask_review(sched, events, oa)
    assert store.get_ticket(a.id).state == State.READY_FOR_REVIEW         # B could stack on it now, with git-spice
    tick(sched, 3)
    B = store.get_ticket(b.id)
    assert B.state == State.OPEN and not B.branch and offer(events, store, b.id, "worker") is None   # it waits for A to land
    why = [n["text"] for n in store.notes(b.id) if "git-spice is not on PATH" in n["text"]]
    assert len(why) == 1 and f"it waits for {a.id} to land" in why[0]

    assert said(project).count(MISSING) == 1                           # said once in the log, not every tick
    assert [x["reason"] for x in asked(events, "supervisor") if "git-spice" in x["reason"]] == [MISSING]


@pytest.mark.skipif(shutil.which("git-spice") is None, reason="git-spice is not installed")
def test_installing_git_spice_takes_effect_without_a_restart(project, no_spice):
    store, events, sched, ms = setup(project)
    a = store.create_ticket(src="planner", title="a", milestone_id=ms.id)
    b = store.create_ticket(src="planner", title="b", milestone_id=ms.id, depends_on=[a.id])
    tick(sched, 2)
    ask_review(sched, events, offer(events, store, a.id, "worker"))
    tick(sched, 2)
    assert offer(events, store, b.id, "worker") is None

    no_spice()                                                             # git-spice installed
    tick(sched, 2)
    ob = offer(events, store, b.id, "worker")
    A, B = store.get_ticket(a.id), store.get_ticket(b.id)
    assert ob is not None and B.stacked_on == a.id and ob["base"] == A.branch     # stacked on A, whose branch was untracked till now
    assert "git-spice is on PATH now" in said(project)
