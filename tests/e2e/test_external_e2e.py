"""External review (``[gate] mode = "external"``): the gate hands an approved branch off for a pull
request, and the ticket follows the pull request to resolution by what the pull-request bridge
publishes (orchestra-bridges, ``github.pr.*``; here the test publishes the bridge's events as the
bridge would, with its ids, sources, subjects and data, and plays the owner who opens, pushes and
merges). The real scheduler, store and git, with machines that speak the protocol
(tests/fake_machine.py): worker/1 does the work, and worker/2 is free once it is handed off, so a
re-engagement that reaches worker/1 reached the worker that wrote the change, not whoever was
free."""

import itertools
import time

import pytest
from conftest import git, handoffs, moves, on_bus
from fake_machine import FakeMachine

from tasking.assignments import ACCEPTED, APPROVED, ASSIGNED, CHANGES_REQUESTED, REVIEW_REQUESTED
from tasking.events import Events
from tasking.model import State
from tasking.scheduler import Scheduler
from tasking.store import LocalStore

R = "acme/service"
N = 12
PULL = f"{R}#{N}"
_ids = itertools.count(1000)


def setup(project, **external):
    """Code mode with external review of ``R``, SAIL-failed failing and SAIL-passed passing."""
    repo = project.repo_root
    toml = repo / "orchestra.toml"
    extra = "".join(f"{k} = {v}\n" for k, v in {"settle": '"0s"', **external}.items())
    text = toml.read_text().replace('work = "tasks"', 'work = "code"').replace('[gate]\n', '[gate]\nmode = "external"\n')
    toml.write_text(text + f'\n[gate.external]\nrepository = "{R}"\nfailing_labels = ["SAIL-failed"]\npassing_labels = ["SAIL-passed"]\n{extra}')
    project.reload_config()
    assert project.config.external_review
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "external review", cwd=repo)
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    t = store.create_ticket(src="planner", title="a change", milestone_id=store.create_milestone(src="planner", name="m").id)
    for login in ("owner", "alice", "sail-bot"):                   # the bridge announces the people it sees
        events.publish("orchestra.machine.started", {"kind": "person", "name": login}, source=f"person.{login}")
    return store, events, t


def pr(events, kind: str, who: str, head: str, branch: str, **data) -> dict:
    """The pull-request bridge's event: the actor as source, the pull request as subject, GitHub's
    ids, metadata only."""
    d = {"repository": R, "pull": N, "login": who, "branch": branch, "base": "main", "head": head, **data}
    return events.publish(f"github.pr.{kind}", d, source=f"person.{who}", subject=PULL, id=f"github:{R}:pr:{N}:{kind}:{next(_ids)}")


def started(project, store, events) -> Scheduler:
    """A scheduler as ``run`` starts one: recovered, and the worker's queue announced."""
    sched = Scheduler(project, store, events)
    sched.recover()
    sched._announce_queues()
    return sched


def until(sched, check, timeout_s=30.0):
    end_at = time.time() + timeout_s
    while time.time() < end_at:
        sched.tick()
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("did not happen in time")


def ticks(sched, n=5):
    for _ in range(n):
        sched.tick()
        time.sleep(0.05)


def in_review(store, tid, not_at=None):
    t = store.get_ticket(tid)
    return t.state == State.IN_EXTERNAL_REVIEW and t.pull_head != not_at


@pytest.fixture
def flow(project):
    """A ticket worked by worker/1, reviewed, approved and handed off; the scheduler and the
    machines running for the test's body."""
    store, events, t = setup(project)
    sched = started(project, store, events)
    with FakeMachine(events, instance="1"):
        until(sched, lambda: in_review(store, t.id))
        with FakeMachine(events, roles=("worker",), instance="2"):            # free from now on
            yield project, store, events, sched, store.get_ticket(t.id)


def test_changes_requested_with_line_comments_go_back_to_the_same_worker_then_merge_closes_it(flow):
    """Acceptance 1 and 5: the hand-off, a review requesting changes with two line comments, the
    same worker instance takes the work back with the feedback as metadata, a new push, the merge:
    DONE. Nothing lands on the base branch here; the hand-offs read planner → worker → reviewer →
    merge_gate → the pull request's reviewer → worker → reviewer → merge_gate, and the scheduler is
    nowhere."""
    project, store, events, sched, t = flow
    c1, branch = t.reviewed_commit, t.branch
    handed = on_bus(events, "gate.handed_off", ticket=t.id)
    assert len(handed) == 1 and handed[0]["source"] == "merge_gate" and handed[0]["data"]["commit"] == c1
    assert handed[0]["causationid"] == on_bus(events, APPROVED, ticket=t.id)[-1]["id"]
    assert t.worker == "worker/1" and t.pull is None and t.pull_head == c1

    pr(events, "opened", "owner", c1, branch, association="OWNER", draft=False)
    review = pr(events, "review_submitted", "alice", c1, branch, review=900, state="changes_requested", comments=2)
    pr(events, "review_commented", "alice", c1, branch, review=900, comment=1, path="src/app.py", line=10)
    pr(events, "review_commented", "alice", c1, branch, review=900, comment=2, path="src/app.py", line=22)
    until(sched, lambda: in_review(store, t.id, not_at=c1))

    back = on_bus(events, CHANGES_REQUESTED, ticket=t.id)
    assert len(back) == 1
    ev = back[0]
    assert ev["source"] == "person.alice" and ev["causationid"] == review["id"] and ev["data"]["to"] == "worker/1"
    d = ev["data"]
    assert d["pull"] == PULL and d["commit"] == c1 and "ends" not in d
    assert d["pull_feedback"]["comments"] == [{"by": "person.alice", "path": "src/app.py", "line": 10, "review": 900},
                                              {"by": "person.alice", "path": "src/app.py", "line": 22, "review": 900}]
    assert "requested changes with 2 line comment(s)" in d["feedback"] and "src/app.py:10, src/app.py:22" in d["feedback"]
    assert d["brief"]["rejected_by"] == "person.alice" and d["brief"]["feedback"] == d["feedback"] and d["brief"]["pull"] == PULL
    taken = [e["source"] for e in on_bus(events, ACCEPTED, assignment=d["assignment"])]
    assert taken[0] == "worker/1"                                                   # the worker that wrote it, though worker/2 was free

    t = store.get_ticket(t.id)
    c2 = t.reviewed_commit
    assert c2 != c1 and git("merge-base", "--is-ancestor", c1, c2, cwd=project.repo_root) == ""     # the same branch, moved on
    assert t.pull == PULL and t.pull_head == c2 and t.pull_rejections == 1 and t.worker_turns == 2 and t.worker == "worker/1"
    assert len(on_bus(events, "gate.handed_off", ticket=t.id)) == 2

    pr(events, "pushed", "owner", c2, branch, previous=c1, forced=False, pusher_from="committer")
    pr(events, "merged", "owner", c2, branch, merge_commit="f" * 40)
    until(sched, lambda: store.get_ticket(t.id).state == State.DONE)
    t = store.get_ticket(t.id)
    assert t.pull_state == "merged"
    assert "Merge" not in git("log", "--format=%s", "main", cwd=project.repo_root)         # the gate landed nothing
    assert not git("branch", "--list", branch, cwd=project.repo_root).strip() and not (project.worktrees_dir / t.id).exists()
    m = moves(store)
    assert m[("APPROVED", "IN_EXTERNAL_REVIEW", "merge_gate")] == 2
    assert m[("IN_EXTERNAL_REVIEW", "REJECTED", "person.alice")] == 1 and m[("IN_EXTERNAL_REVIEW", "DONE", "person.owner")] == 1
    assert m[("REJECTED", "IN_PROGRESS", "worker")] == 1

    assert handoffs(events, t.id) == [("planner", "ticket.created", "worker"), ("worker", REVIEW_REQUESTED, "reviewer"),
                                      ("reviewer", APPROVED, "merge_gate"), ("merge_gate", "gate.handed_off", "person.alice"),
                                      ("person.alice", CHANGES_REQUESTED, "worker"), ("worker", REVIEW_REQUESTED, "reviewer"),
                                      ("reviewer", APPROVED, "merge_gate")]
    evs = events.bus.read(0, limit=100000)[0]
    assert not [e for e in evs if e["source"] == "task_scheduler"]
    assert [e["type"] for e in evs if e.get("subject") == t.id and e["source"] == "worker"] == [ASSIGNED]
    assert not [e for e in evs if e["type"] in ("ticket.transitioned", "ticket.note_added")]


def test_a_failing_label_reengages_the_worker_and_only_the_merge_closes_the_ticket(flow):
    """Acceptance 2: the owner adds SAIL (nothing), the pipeline's account adds SAIL-failed: the
    worker that wrote it takes it back. After the new push, SAIL-passed and an approval leave the
    ticket in external review; the merge closes it."""
    _project, store, events, sched, t = flow
    c1, branch = t.reviewed_commit, t.branch
    pr(events, "opened", "owner", c1, branch)
    pr(events, "labelled", "owner", c1, branch, label="SAIL")
    ticks(sched)
    assert store.get_ticket(t.id).state == State.IN_EXTERNAL_REVIEW and not on_bus(events, CHANGES_REQUESTED, ticket=t.id)
    failed = pr(events, "labelled", "sail-bot", c1, branch, label="SAIL-failed")
    until(sched, lambda: in_review(store, t.id, not_at=c1))
    back = on_bus(events, CHANGES_REQUESTED, ticket=t.id)
    assert [(e["source"], e["causationid"], e["data"]["to"]) for e in back] == [("person.sail-bot", failed["id"], "worker/1")]
    assert back[0]["data"]["pull_feedback"]["labels"] == [{"by": "person.sail-bot", "label": "SAIL-failed"}]
    assert on_bus(events, ACCEPTED, assignment=back[0]["data"]["assignment"])[0]["source"] == "worker/1"

    c2 = store.get_ticket(t.id).reviewed_commit
    pr(events, "pushed", "owner", c2, branch, previous=c1)
    pr(events, "labelled", "sail-bot", c2, branch, label="SAIL-passed")
    pr(events, "review_submitted", "alice", c2, branch, review=901, state="approved", comments=0)
    ticks(sched, 10)
    t = store.get_ticket(t.id)
    assert t.state == State.IN_EXTERNAL_REVIEW and len(on_bus(events, CHANGES_REQUESTED, ticket=t.id)) == 1
    notes = [n["text"] for n in store.notes(t.id)]
    assert any("passing label SAIL-passed" in n for n in notes) and any("approved" in n and "only a merge" in n for n in notes)
    pr(events, "merged", "owner", c2, branch)
    until(sched, lambda: store.get_ticket(t.id).state == State.DONE)


def test_closed_without_a_merge_sends_the_ticket_back_to_waiting_and_reopened_resumes(flow):
    """Acceptance 3: closed unmerged, the ticket waits (BLOCKED, with the reason) for a planner,
    and the merge gate, whose hand-off it was, asks the coordinator; reopened, it follows the pull
    request again."""
    _project, store, events, sched, t = flow
    pr(events, "opened", "owner", t.reviewed_commit, t.branch)
    pr(events, "closed", "alice", t.reviewed_commit, t.branch, merged=False)
    until(sched, lambda: store.get_ticket(t.id).state == State.BLOCKED)
    t = store.get_ticket(t.id)
    assert t.pull_state == "closed" and t.blocked_reason.startswith(f"its pull request {PULL} was closed without a merge by person.alice")
    asked = [e for e in on_bus(events, "task.attention_needed", ticket=t.id)]
    assert [(e["source"], e["data"]["to"]) for e in asked] == [("merge_gate", "coordinator")]
    assert not on_bus(events, CHANGES_REQUESTED, ticket=t.id) and not store.assignments()
    pr(events, "reopened", "alice", t.reviewed_commit, t.branch)
    until(sched, lambda: in_review(store, t.id))
    assert store.get_ticket(t.id).pull_state == "open"


def test_feedback_on_an_older_head_is_noted_not_acted_on(flow):
    """Acceptance 4: after a round of feedback and the new push, a late review and a failing label
    about the old head change nothing but the notes; feedback on the new head does re-engage."""
    _project, store, events, sched, t = flow
    c1, branch = t.reviewed_commit, t.branch
    pr(events, "opened", "owner", c1, branch)
    pr(events, "check_completed", "github-actions", c1, branch, check="ci", conclusion="failure", via="check_run", app="github-actions")
    until(sched, lambda: in_review(store, t.id, not_at=c1))
    c2 = store.get_ticket(t.id).reviewed_commit
    pr(events, "pushed", "owner", c2, branch, previous=c1)
    pr(events, "review_submitted", "alice", c1, branch, review=902, state="changes_requested", comments=0)
    pr(events, "labelled", "sail-bot", c1, branch, label="SAIL-failed")
    ticks(sched, 10)
    t = store.get_ticket(t.id)
    assert t.state == State.IN_EXTERNAL_REVIEW and t.pull_rejections == 1 and len(on_bus(events, CHANGES_REQUESTED, ticket=t.id)) == 1
    stale = [n for n in store.notes(t.id) if "stale" in n["text"]]
    assert [n["author"] for n in stale] == ["person.alice", "person.sail-bot"] and all(c1[:10] in n["text"] for n in stale)
    pr(events, "review_submitted", "alice", c2, branch, review=903, state="changes_requested", comments=0)
    until(sched, lambda: len(on_bus(events, CHANGES_REQUESTED, ticket=t.id)) == 2)
    assert on_bus(events, CHANGES_REQUESTED, ticket=t.id)[-1]["data"]["commit"] == c2


def test_a_merge_while_the_worker_has_it_back_releases_it_and_closes_the_ticket(project):
    """Merge is final: a pull request merged while the worker is on the feedback releases it, and
    the ticket is DONE."""
    store, events, t = setup(project)
    sched = started(project, store, events)
    with FakeMachine(events, instance="1", heartbeat=0.2) as m:                  # works slowly: still on it when the merge comes
        m.finish.set()
        until(sched, lambda: in_review(store, t.id))
        m.finish.clear()
        t = store.get_ticket(t.id)
        pr(events, "opened", "owner", t.reviewed_commit, t.branch)
        pr(events, "review_submitted", "alice", t.reviewed_commit, t.branch, review=904, state="changes_requested", comments=0)
        until(sched, lambda: store.get_ticket(t.id).state == State.IN_PROGRESS)
        pr(events, "merged", "owner", t.reviewed_commit, t.branch)
        until(sched, lambda: store.get_ticket(t.id).state == State.DONE)
    assert not store.assignments()
    released = [e["data"]["reason"] for e in on_bus(events, "task.released", ticket=t.id)]
    assert any("was merged by person.owner" in r for r in released)


def test_a_merge_while_the_scheduler_was_down_is_applied_when_it_starts(project):
    """It reads the bus on from where it stopped: nothing the bridge published is lost to a
    restart, and nothing is applied twice."""
    store, events, t = setup(project)
    sched = started(project, store, events)
    with FakeMachine(events, instance="1"):
        until(sched, lambda: in_review(store, t.id))
    t = store.get_ticket(t.id)
    pr(events, "opened", "owner", t.reviewed_commit, t.branch)
    ticks(sched, 2)
    pr(events, "merged", "owner", t.reviewed_commit, t.branch)              # nobody follows the bus now
    again = started(project, store, events)
    until(again, lambda: store.get_ticket(t.id).state == State.DONE)
    third = started(project, store, events)
    ticks(third, 2)
    assert sum(n["text"].startswith("person.owner merged") for n in store.notes(t.id)) == 1


def test_a_reviews_line_comments_are_gathered_before_the_worker_is_reengaged(project):
    """The bridge publishes a review and then its line comments; the worker is re-engaged once,
    with all of them, even when they come after the review (``settle``)."""
    store, events, t = setup(project, settle='"1s"')
    sched = started(project, store, events)
    with FakeMachine(events, instance="1"):
        until(sched, lambda: in_review(store, t.id))
    t = store.get_ticket(t.id)
    c1, branch = t.reviewed_commit, t.branch
    pr(events, "opened", "owner", c1, branch)
    pr(events, "review_submitted", "alice", c1, branch, review=905, state="changes_requested", comments=2)
    ticks(sched, 2)
    pr(events, "review_commented", "alice", c1, branch, review=905, comment=3, path="a.py", line=1)
    ticks(sched, 2)
    pr(events, "review_commented", "alice", c1, branch, review=905, comment=4, path="b.py", line=2)
    until(sched, lambda: on_bus(events, CHANGES_REQUESTED, ticket=t.id))
    ticks(sched, 3)
    back = on_bus(events, CHANGES_REQUESTED, ticket=t.id)
    assert len(back) == 1 and [c["path"] for c in back[0]["data"]["pull_feedback"]["comments"]] == ["a.py", "b.py"]
