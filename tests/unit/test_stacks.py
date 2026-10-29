"""The stacking policy (tasking/stacks.py) on the record alone: when a ticket may start on its
dependency's branch, and the setting that turns it on."""

import pytest
from conftest import git

from tasking.config import Config, ConfigError
from tasking.events import Events
from tasking.git import Git
from tasking.model import State
from tasking.spice import Spice
from tasking.stacks import MISSING, Stacks
from tasking.store import LocalStore


def test_the_setting():
    assert not Config.from_toml('[project]\nwork = "code"\n').stacked
    assert not Config.from_toml('[project]\nwork = "tasks"\n[gate]\nstacking = "git-spice"\n').stacked      # code mode only
    c = Config.from_toml('[project]\nwork = "code"\n[gate]\nstacking = "git-spice"\nstack_depth = 2\n')
    assert c.stacked and c.stack_depth == 2
    assert not Config.from_toml('[project]\nwork = "code"\n[gate]\nstacking = "off"\n').stacked
    with pytest.raises(ConfigError):
        Config.from_toml('[project]\nwork = "code"\n[gate]\nstacking = "gh-stack"\n')


@pytest.fixture
def env(project, monkeypatch):
    monkeypatch.setattr(Spice, "available", lambda self: True)          # on the record alone: git-spice is never run here
    toml = project.repo_root / "orchestra.toml"
    toml.write_text(toml.read_text().replace('work = "tasks"', 'work = "code"').replace("[gate]\n", '[gate]\nstacking = "git-spice"\nstack_depth = 2\n'))
    project.reload_config()
    events = Events.for_project(project, source="merge_gate")
    store = LocalStore(project, events)
    stacks = Stacks(project, store, Git(project.repo_root), store, events, log=lambda _m: None)
    ms = store.create_milestone(src="planner", name="m")

    def ticket(title, state=State.OPEN, deps=(), reviewed=True, stacked_on=None):
        t = store.create_ticket(src="planner", title=title, milestone_id=ms.id, depends_on=list(deps))
        branch = f"orchestra/{t.id}-{title}"
        git("branch", branch, "main", cwd=project.repo_root)
        return store.update_ticket(t.id, branch=branch, reviewed_commit="c" if reviewed else None, stacked_on=stacked_on) \
            if state == State.OPEN else _at(store, t.id, state, branch, reviewed, stacked_on)

    def ready(t):
        return stacks.ready(store.get_ticket(t.id), store.tickets_by_id(), store.milestones_by_id())
    return store, stacks, ticket, ready


def _at(store, tid, state, branch, reviewed, stacked_on):
    d = store._t()
    d["tickets"][tid].update(state=state.value, branch=branch, reviewed_commit="c" if reviewed else None, stacked_on=stacked_on)
    from tasking.store.files import write
    write(store.project.tickets_file, d)
    return store.get_ticket(tid)


def test_a_ticket_stacks_on_the_one_dependency_whose_work_stands_for_review(env):
    store, stacks, ticket, ready = env
    a = ticket("a", State.READY_FOR_REVIEW)
    b = store.create_ticket(src="planner", title="b", milestone_id=a.milestone_id, depends_on=[a.id])
    assert ready(b) and stacks.parent(b, store.tickets_by_id()).id == a.id
    for state in (State.IN_PROGRESS, State.REJECTED, State.MERGING, State.CANCELLED, State.BLOCKED):
        _at(store, a.id, state, a.branch, True, None)
        assert not ready(b), state                         # being changed, landing, or not standing for review
    _at(store, a.id, State.APPROVED, a.branch, False, None)
    assert not ready(b)                                    # nothing reviewed to stack on yet


def test_two_unlanded_dependencies_wait_and_a_deep_stack_waits(env):
    store, stacks, ticket, ready = env
    a = ticket("a", State.APPROVED)
    x = ticket("x", State.APPROVED)
    two = store.create_ticket(src="planner", title="two", milestone_id=a.milestone_id, depends_on=[a.id, x.id])
    assert not ready(two)                                  # a branch has one parent
    _at(store, x.id, State.DONE, x.branch, True, None)
    assert ready(two)                                      # the other landed: it stacks on a
    b = ticket("b", State.APPROVED, deps=[a.id], stacked_on=a.id)
    c = store.create_ticket(src="planner", title="c", milestone_id=a.milestone_id, depends_on=[b.id])
    assert stacks.depth(b, store.tickets_by_id()) == 2 and not ready(c)    # stack_depth = 2: two unlanded below already
    _at(store, a.id, State.DONE, a.branch, True, None)
    assert ready(c)


def test_the_gate_and_the_offers_see_the_parent(env):
    store, stacks, ticket, _ready = env
    a = ticket("a", State.APPROVED)
    b = ticket("b", State.APPROVED, deps=[a.id], stacked_on=a.id)
    assert stacks.base(b) == a.branch and not stacks.may_land(b) and stacks.may_land(a)
    _at(store, a.id, State.DONE, a.branch, True, None)
    assert stacks.base(store.get_ticket(b.id)) == "main"    # landed: its branch moves onto the base next tick


def test_without_git_spice_a_ticket_that_would_stack_waits_and_says_why_once(env, monkeypatch):
    store, stacks, ticket, ready = env
    said = []
    stacks.log = said.append
    a = ticket("a", State.READY_FOR_REVIEW)
    b = store.create_ticket(src="planner", title="b", milestone_id=a.milestone_id, depends_on=[a.id])
    plain = store.create_ticket(src="planner", title="plain", milestone_id=a.milestone_id)
    monkeypatch.setattr(Spice, "available", lambda self: False)
    for _ in range(3):
        assert not ready(b) and ready(plain)               # waits for a to land, as with stacking off; the plain one is ready
    assert said.count(f"stacking: {MISSING}") == 1 and sum(f"waits for {a.id} to land" in m for m in said) == 1
    assert [n["text"] for n in store.notes(b.id) if "git-spice" in n["text"]] == [
        f"{b.id} would branch from {a.branch} ({a.id}, not landed yet), but git-spice is not on PATH: it waits for {a.id} to land"]
    _at(store, a.id, State.DONE, a.branch, True, None)
    assert ready(b)                                        # landed: it starts from the base
    monkeypatch.setattr(Spice, "available", lambda self: True)
    assert stacks.usable() and "git-spice is on PATH now" in said[-1]


def test_without_git_spice_a_branch_from_the_base_and_git_spice_never_asked(env, monkeypatch):
    store, stacks, _ticket, _ready = env
    monkeypatch.setattr(Spice, "available", lambda self: False)
    monkeypatch.setattr(Spice, "run", lambda self, *a, **k: pytest.fail(f"git-spice asked {a}"))
    ms = store.create_milestone(src="planner", name="n")
    t = store.create_ticket(src="planner", title="t", milestone_id=ms.id)
    stacks.prepare(t, "orchestra/t")
    g = Git(store.project.repo_root)
    assert g.rev_parse("orchestra/t") == g.rev_parse("main")
    stacks.prepare(t, "orchestra/t")                       # made already: still nothing asked
    stacks.tick()
