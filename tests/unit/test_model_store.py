import pytest

from tasking.config import Config
from tasking.events import Events
from tasking.model import State, TransitionError, check_transition
from tasking.store import Conflict, LocalStore, StoreError


@pytest.fixture
def store(project):
    return LocalStore(project, Events.for_project(project, source="planner"))


def test_transitions():
    check_transition(State.OPEN, State.IN_PROGRESS)
    with pytest.raises(TransitionError):
        check_transition(State.IN_PROGRESS, State.DONE)       # done only by an approval, a landing or a merged pull request
    with pytest.raises(TransitionError):
        check_transition(State.DONE, State.OPEN)
    # only a waiting ticket may be cancelled: an active one is held, and settles first
    for waiting in (State.OPEN, State.REJECTED, State.READY_FOR_REVIEW, State.APPROVED, State.BLOCKED, State.IN_EXTERNAL_REVIEW):
        check_transition(waiting, State.CANCELLED)
    for active in (State.IN_PROGRESS, State.IN_REVIEW, State.MERGING):
        with pytest.raises(TransitionError):
            check_transition(active, State.CANCELLED)


def test_milestone_rules_and_ids(store):
    with pytest.raises(StoreError):
        store.create_ticket(src="planner", title="x", milestone_id="M9")
    m = store.create_milestone(src="planner", name="first")
    assert m.id == "M1"
    t = store.create_ticket(src="planner", title="Add thing", milestone_id=m.id)
    assert t.id == "T-1" and t.state == State.OPEN
    with pytest.raises(StoreError):
        store.close_milestone(m.id)  # unfinished ticket
    t = store.transition(t.id, t.version, State.CANCELLED, by="worker")
    store.close_milestone(m.id)
    with pytest.raises(StoreError):
        store.create_ticket(src="planner", title="y", milestone_id=m.id)
    with pytest.raises(StoreError):
        store.update_milestone(m.id, status="open")


def test_version_conflict_and_blocked_reason(store):
    m = store.create_milestone(src="planner", name="m")
    t = store.create_ticket(src="planner", title="a", milestone_id=m.id)
    with pytest.raises(Conflict):
        store.transition(t.id, t.version + 5, State.IN_PROGRESS, by="worker")          # a transition from a stale read
    t = store.transition(t.id, t.version, State.IN_PROGRESS, by="worker")
    with pytest.raises(StoreError):
        store.transition(t.id, t.version, State.BLOCKED, by="worker")
    store.transition(t.id, t.version, State.BLOCKED, blocked_reason="need creds", by="worker")


def test_operator_capacity_is_the_ceiling_a_planner_dials_under(store):
    configured = store.project.config.workers
    assert store.capacity()["max_workers"] == configured
    cap = store.set_operator_capacity(configured + 3, None)
    assert cap["max_workers"] == configured + 3 and cap["workers"] == cap["max_workers"]   # live, above the file's number
    store.set_capacity(1, None, note="slow today", by="coordinator")
    cap = store.capacity()
    assert cap["workers"] == 1 and cap["max_workers"] == configured + 3 and cap["note"] == "slow today"   # the dial sits under the ceiling
    store.set_capacity(99, None, by="coordinator")
    assert store.capacity()["workers"] == configured + 3                                    # and never above it
    cap = store.set_operator_capacity(0, 0)
    assert cap["workers"] == 0 and cap["reviewers"] == 0
    cap = store.set_operator_capacity(None, None)
    assert cap["max_workers"] == configured                                                 # back to orchestra.toml


def test_the_runtime_record_has_named_methods(store):
    assert store.scheduler() is None
    store.set_scheduler(4242, 1.0)
    assert store.scheduler() == {"pid": 4242, "started_at": 1.0}
    store.clear_scheduler(1)                                     # not ours: kept
    assert store.scheduler()["pid"] == 4242
    store.clear_scheduler(4242)
    assert store.scheduler() is None
    store.record_build({"id": "b1", "commit": "c"})
    assert store.builds() == {"b1": {"id": "b1", "commit": "c"}}
    assert store.set_staging(status="running", pid=7)["pid"] == 7 and store.staging()["status"] == "running"
    assert store.snapshot()["probes"] == [] and store.snapshot()["worktrees"] == []


def test_config_durations():
    cfg = Config.from_toml('[tasks]\nworkers = 2\n[build]\ntimeout = "2m"\n[[gate.unit]]\nname="u"\ncommand="true"\n')
    assert cfg.build_timeout == 120 and cfg.workers == 2 and cfg.unit[0].command == "true"
    assert cfg.tick == 3.0 and Config.from_toml('[tasks]\ntick = "500ms"\n').tick == 0.5


def test_effective_priority_propagates_and_ages():
    from datetime import UTC, datetime, timedelta

    from tasking.model import Ticket, effective_priorities

    old = (datetime.now(UTC) - timedelta(days=5)).isoformat(timespec="seconds")
    a = Ticket(id="A", title="a", milestone_id="M1", priority=5)
    b = Ticket(id="B", title="b", milestone_id="M1", priority=1, depends_on=["A"])
    c = Ticket(id="C", title="c", milestone_id="M1", priority=6, created_at=old)
    done = Ticket(id="D", title="d", milestone_id="M1", priority=1, state=State.DONE, depends_on=["C"])
    odd = Ticket(id="E", title="e", milestone_id="M1", priority=6, created_at="not a time")
    eff = effective_priorities({"A": a, "B": b, "C": c, "D": done, "E": odd}, aging_days_per_level=2)
    assert eff["A"]["effective"] == 1 and eff["A"]["inherited_from"] == "B"   # prerequisite inherits
    assert eff["B"]["effective"] == 1 and eff["B"]["inherited_from"] is None
    assert eff["C"]["effective"] == 4 and eff["C"]["age_levels"] == 2          # 5 days / 2 = 2 levels
    assert "D" not in eff                                                      # terminal tickets are ignored
    assert eff["E"]["age_levels"] == 0 and eff["E"]["effective"] == 6          # an unreadable created_at does not age
