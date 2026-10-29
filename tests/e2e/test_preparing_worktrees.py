"""A worktree that takes long to be ready (a monorepo's ``git worktree add``, a pool reset, a slow
``[tasks] worktree_setup``) is made off the scheduler's loop (tasking/prepare.py): the ticket is
``PREPARING`` meanwhile, the loop goes on with other tickets, and the offer goes out when the
worktree is ready. A setup that fails sends it back to waiting with the setup's back-off; a planner
may cancel it; a restart or a shutdown sends it back where it waited. The real scheduler, store
and git; the test plays the machines by hand, or the fake machine does."""

import threading
import time
from pathlib import Path

import pytest
from conftest import asked, git, moves, wait_for

from tasking.api import handle
from tasking.assignments import ACCEPTED, ASSIGNED, REVIEW_REQUESTED, pass_on
from tasking.model import State
from tasking.prepare import GRACE_S
from tasking.scheduler import Scheduler
from tasking.status import collect, render

SLOW = "case $ORCHESTRA_WORKTREE in */T-1) sleep 6;; esac"                     # only T-1's own worktree is slow
SLOW_FAILS = "case $ORCHESTRA_WORKTREE in */T-1) sleep 3; echo no >&2; exit 4;; esac"


def offers(events, tid):
    return [e for e in events.after(0)[0] if e["type"] == ASSIGNED and e.get("ticket") == tid]


def claim(events, offer, instance="worker/1"):
    events.publish(ACCEPTED, {"assignment": offer["assignment"], "ticket": offer["ticket"], "role": offer["role"]},
                   source=instance, subject=offer["ticket"])


def two_tickets(store):
    ms = store.create_milestone(src="planner", name="m")
    return [store.create_ticket(src="planner", title=f"ticket {i}", milestone_id=ms.id) for i in (1, 2)]


def test_a_slow_worktree_is_preparing_while_the_scheduler_goes_on_and_is_offered_when_ready(owner_layout):
    project, store, events, _code = owner_layout(f'worktree_setup = "{SLOW}"')
    t1, t2 = two_tickets(store)
    sched = Scheduler(project, store, events)
    sched.recover()
    started = time.monotonic()
    sched.tick()
    assert time.monotonic() - started < GRACE_S + 1.5               # the tick waited its grace, not the setup
    t = store.get_ticket(t1.id)
    assert t.state == State.PREPARING and t.preparing_from == "OPEN" and not offers(events, t1.id)
    assert [p["ticket"] for p in store.preparing().values() if p["ticket"] == t1.id]
    # meanwhile: T-2's worktree is ready (offered now or on the next tick), claimed, and recorded as held
    wait_for(lambda: (sched.tick() or True) and bool(offers(events, t2.id)), timeout_s=3)
    claim(events, offers(events, t2.id)[0])
    started = time.monotonic()
    sched.tick()
    assert time.monotonic() - started < 1.5                          # nothing to wait for: the loop is not held by T-1
    assert store.get_ticket(t2.id).state == State.IN_PROGRESS and store.get_ticket(t1.id).state == State.PREPARING
    # status and the page say so, with how long
    text = render(collect(project, store, events))
    assert "preparing worktree (worker)" in text and t1.id in text and "PREPARING" in text
    view = next(v for v in store.ticket_views()["tickets"] if v["id"] == t1.id)
    assert view["preparing"]["role"] == "worker" and "prepared" in view["why_waiting"]
    # ready: back to OPEN and offered, then held
    wait_for(lambda: (sched.tick() or True) and bool(offers(events, t1.id)), timeout_s=15)
    assert store.get_ticket(t1.id).state == State.OPEN and store.preparing() == {}
    o = offers(events, t1.id)[0]
    assert (project.worktrees_dir / t1.id).is_dir() and o["workspace"] == str(project.worktrees_dir / t1.id)
    claim(events, o, "worker/2")
    sched.tick()
    t = store.get_ticket(t1.id)
    assert t.state == State.IN_PROGRESS and t.owner == "worker/2" and t.preparing_from is None
    m = moves(store)                                                 # counted as dispatch's (the coordinator's), there and back
    assert m[("OPEN", "PREPARING", "coordinator")] == m[("PREPARING", "OPEN", "coordinator")] >= 1
    assert not [e for e in events.after(0)[0] if "prepar" in e["type"]]   # nothing published for it: the offer is


def test_a_failing_slow_setup_sends_the_ticket_back_and_waits_out_the_back_off(owner_layout):
    project, store, events, _code = owner_layout(f'worktree_setup = "{SLOW_FAILS}"')
    t1, _t2 = two_tickets(store)
    sched = Scheduler(project, store, events)
    sched.recover()
    sched.tick()
    assert store.get_ticket(t1.id).state == State.PREPARING
    wait_for(lambda: (sched.tick() or True) and store.get_ticket(t1.id).state == State.OPEN, timeout_s=15)
    assert store.preparing() == {} and not offers(events, t1.id)
    notes = [n["text"] for n in store.notes(t1.id)]
    assert any("could not be prepared" in n and "back to OPEN" in n for n in notes), notes
    for _ in range(3):                                               # the back-off holds: not prepared again, not PREPARING
        sched.tick()
    assert store.get_ticket(t1.id).state == State.OPEN and store.preparing() == {}
    log = (project.logs_dir / "scheduler.log").read_text()
    assert log.count("worktree setup: running in") == 2 and "(exit 4)" in log   # T-1's once, T-2's once
    assert len([a for a in asked(events, "supervisor") if "worktree_setup failed" in a["reason"]]) == 1


def test_a_planner_cancels_a_preparing_ticket_and_its_worktree_goes(owner_layout):
    project, store, events, _code = owner_layout(f'worktree_setup = "{SLOW}"')
    t1, _t2 = two_tickets(store)
    sched = Scheduler(project, store, events)
    sched.recover()
    sched.tick()
    assert store.get_ticket(t1.id).state == State.PREPARING
    out = handle(project, store, events, "cancel_ticket", {"id": t1.id, "reason": "not needed"}, role="planner")
    assert out["ok"] and store.get_ticket(t1.id).state == State.CANCELLED
    wait_for(lambda: (sched.tick() or True) and not any(p["ticket"] == t1.id for p in store.preparing().values()), timeout_s=15)
    sched.tick()
    assert not offers(events, t1.id) and store.get_ticket(t1.id).state == State.CANCELLED
    assert store.worktree(t1.id) is None and not (project.worktrees_dir / t1.id).exists()   # dropped as any cancelled ticket's


def test_a_restart_mid_preparation_sends_it_back_and_a_shutdown_ends_the_setup(owner_layout):
    project, store, events, _code = owner_layout(f'worktree_setup = "{SLOW}"')
    t1, _t2 = two_tickets(store)
    first = Scheduler(project, store, events)
    first.recover()
    first.tick()
    assert store.get_ticket(t1.id).state == State.PREPARING
    first.setup.stop()                                               # the first one died (its setup with it)
    second = Scheduler(project, store, events)
    second.recover()
    assert store.get_ticket(t1.id).state == State.OPEN and store.preparing() == {}
    assert any("restarted" in n["text"] and "back to OPEN" in n["text"] for n in store.notes(t1.id))
    # the second prepares it again (the half-set-up worktree is set up again), and shuts down mid-way
    th = threading.Thread(target=second.run, daemon=True)
    th.start()
    wait_for(lambda: store.get_ticket(t1.id).state == State.PREPARING, timeout_s=15)
    began = time.monotonic()
    second.stop.set()
    th.join(timeout=20)
    assert not th.is_alive() and time.monotonic() - began < 8         # the setup was ended, not waited out
    assert store.get_ticket(t1.id).state == State.OPEN and store.preparing() == {}
    assert any("shut down" in n["text"] for n in store.notes(t1.id))
    assert not [a for a in asked(events, "supervisor") if "worktree_setup failed" in a["reason"]]   # a stop is no failure


def test_with_the_pool_slow_resets_are_prepared_off_the_loop_for_workers_and_reviewers(owner_layout, monkeypatch):
    """A worker's turn is PREPARING while its slot is reset; a reviewer's, offered from the queue,
    leaves the ticket READY_FOR_REVIEW and shows from the record. The slot is held throughout."""
    project, store, events, _code = owner_layout("reuse_worktrees = true\nworktree_pool = 3")
    from tasking.pool import Pool
    reset = Pool.reset

    def slow_reset(self, path, branch):
        time.sleep(GRACE_S + 1)                                      # a monorepo's reset
        reset(self, path, branch)
    monkeypatch.setattr(Pool, "reset", slow_reset)
    ms = store.create_milestone(src="planner", name="m")
    t = store.create_ticket(src="planner", title="one", milestone_id=ms.id)
    sched = Scheduler(project, store, events)
    sched.recover()
    sched.tick()
    assert store.get_ticket(t.id).state == State.PREPARING
    assert [s for s in store.pool().values() if not s["free"]]       # the slot is held while it is reset
    wait_for(lambda: (sched.tick() or True) and bool(offers(events, t.id)), timeout_s=15)
    offer = offers(events, t.id)[0]
    claim(events, offer)
    sched.tick()
    assert store.get_ticket(t.id).state == State.IN_PROGRESS
    ws = offer["workspace"]
    (Path(ws) / "WORK.md").write_text("done\n")
    git("add", "-A", cwd=ws)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "work", cwd=ws)
    sha = git("rev-parse", "HEAD", cwd=ws).strip()
    data = pass_on(offer, REVIEW_REQUESTED, source="worker/1", review={"summary": "did it", "commit": sha, "worker": "worker/1"})
    data["assignment"] = None                                        # an offer the record cannot stand behind: the queue offers the review
    events.publish(REVIEW_REQUESTED, data, source="worker/1", subject=t.id)
    sched.tick()                                                     # recorded, and the review prepared from the queue
    assert store.get_ticket(t.id).state == State.READY_FOR_REVIEW
    preps = list(store.preparing().values())
    assert [p["role"] for p in preps] == ["reviewer"] and preps[0]["ticket"] == t.id
    assert any(s["holder"] == preps[0]["id"] for s in store.pool().values())
    text = render(collect(project, store, events))
    assert "preparing worktree (reviewer)" in text
    wait_for(lambda: (sched.tick() or True) and any(e["role"] == "reviewer" for e in offers(events, t.id)), timeout_s=15)
    assert store.get_ticket(t.id).state == State.READY_FOR_REVIEW and store.preparing() == {}
    review = [e for e in offers(events, t.id) if e["role"] == "reviewer"][0]
    assert git("rev-parse", "HEAD", cwd=review["workspace"]).strip() == sha
    assert not [m for m in moves(store) if "PREPARING" in m and m[0] == "READY_FOR_REVIEW"]
