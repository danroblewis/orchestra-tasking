"""Stacked branches (``[gate] stacking = "git-spice"``, goal G7), end to end: the real scheduler,
store, git, gate and git-spice in a fresh repository. The machines are played by the test, turn by
turn, through the protocol on the bus (claim, commit, ask for a review, approve or ask for changes),
so each step is seen as it happens; the last test runs the fake machine over the pool instead. In
external review the test publishes the pull-request bridge's events as the bridge would and plays
the owner who merges (a squash merge, on the base or only on the remote)."""

import itertools
import shutil
import subprocess
import time

import pytest
from conftest import git, on_bus

from tasking.assignments import (
    ACCEPTED,
    APPROVED,
    CHANGES_REQUESTED,
    OFFERS,
    REVIEW_REQUESTED,
    pass_on,
)
from tasking.cli import main as cli
from tasking.events import Events
from tasking.measures import RESTACKS, counters
from tasking.model import State
from tasking.scheduler import Scheduler
from tasking.stacks import listing
from tasking.store import LocalStore

pytestmark = pytest.mark.skipif(shutil.which("git-spice") is None, reason="git-spice is not installed")
R, _ids = "acme/service", itertools.count(1)


def setup(project, mode="land", pool=False):
    repo = project.repo_root
    toml = repo / "orchestra.toml"
    text = toml.read_text().replace('work = "tasks"', 'work = "code"').replace("[gate]\n", f'[gate]\nmode = "{mode}"\nstacking = "git-spice"\n')
    if pool:
        text = text.replace("[tasks]\n", "[tasks]\nreuse_worktrees = true\nworktree_pool = 4\n")
    if mode == "external":
        text += f'\n[gate.external]\nrepository = "{R}"\nsettle = "0s"\n'
    toml.write_text(text)
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


def tick(sched, n=1):
    for _ in range(n):
        sched.tick()
        if sched.gate_thread:
            sched.gate_thread.join(60)
            sched._reap()


def until(sched, check, timeout_s=30.0):
    end = time.time() + timeout_s
    while time.time() < end:
        tick(sched)
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("did not happen in time")


def offer_for(sched, events, store, tid, role):
    """The offer that stands for ``tid``'s ``role`` (ticking until there is one), as the bus carried it."""
    found = []

    def look():
        held = {a["id"] for a in store.assignments().values() if a["ticket"] == tid and a["role"] == role and a.get("instance") is None}
        found[:] = [e for e in events.after(0)[0] if e["type"] in OFFERS and e.get("assignment") in held]
        return bool(found)
    until(sched, look)
    return found[-1]


def claim(sched, events, offer, who):
    events.publish(ACCEPTED, {"assignment": offer["assignment"], "ticket": offer["ticket"], "role": offer["role"]}, source=who,
                   subject=offer["ticket"])
    tick(sched)


def work(sched, events, store, tid, files: dict[str, str], who="worker/1"):
    """``who`` takes the worker's offer for ``tid``, writes ``files`` in its worktree, commits and asks
    for a review. The offer it worked from and the review it asked for."""
    offer = offer_for(sched, events, store, tid, "worker")
    claim(sched, events, offer, who)
    cwd = offer["workspace"]
    for name, text in files.items():
        with open(f"{cwd}/{name}", "w") as f:
            f.write(text)
    git("add", "-A", cwd=cwd)
    git("commit", "-qm", f"{tid}: {', '.join(files)}", cwd=cwd)
    head = git("rev-parse", "HEAD", cwd=cwd).strip()
    review = pass_on(offer, REVIEW_REQUESTED, source=who, review={"summary": "done", "commit": head, "test_output": None, "worker": who})
    events.publish(REVIEW_REQUESTED, review, source=who, subject=tid)
    tick(sched)
    return offer, review


def verdict(sched, events, store, tid, approve=True, who="reviewer/1"):
    offer = offer_for(sched, events, store, tid, "reviewer")
    claim(sched, events, offer, who)
    commit = (offer.get("review") or {}).get("commit") or store.get_ticket(tid).reviewed_commit
    if approve:
        events.publish(APPROVED, {"ends": offer["assignment"], "ticket": tid, "role": "reviewer", "summary": "yes", "commit": commit},
                       source=who, subject=tid)
    else:
        offer = {**offer, "review": {**(offer.get("review") or {}), "worker": offer.get("review", {}).get("worker") or store.get_ticket(tid).worker}}
        events.publish(CHANGES_REQUESTED, pass_on(offer, CHANGES_REQUESTED, source=who, summary="no", feedback="redo", commit=commit),
                       source=who, subject=tid)
    tick(sched)


def head(project, ref):
    return git("rev-parse", ref, cwd=project.repo_root).strip()


def ancestor(project, a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=project.repo_root, check=False).returncode == 0


def restacks(store):
    return {dict(labels)["outcome"]: n for (name, labels), n in counters(store._t()["metric_counts"]).items() if name == RESTACKS}


def spice_base(project, branch):
    import json
    out = subprocess.run(["git-spice", "--no-prompt", "log", "short", "--all", "--json"], cwd=project.repo_root, capture_output=True,
                         text=True, check=True).stdout
    return next(json.loads(x)["down"]["name"] for x in out.splitlines() if x.strip() and json.loads(x)["name"] == branch)


def test_a_tree_stacks_restacks_and_lands_bottom_first(project):
    """B and C depend on A: both branch from A's branch once A asks for a review (a diverging tree,
    tracked by git-spice with A as their base); a new commit on A restacks both, by the merge gate;
    the gate lands A first, then moves B and C onto the base, tests them again and lands them."""
    store, events, sched, ms = setup(project)
    a = store.create_ticket(src="planner", title="a", milestone_id=ms.id)
    b = store.create_ticket(src="planner", title="b", milestone_id=ms.id, depends_on=[a.id])
    c = store.create_ticket(src="planner", title="c", milestone_id=ms.id, depends_on=[a.id])
    tick(sched, 2)
    assert {x["ticket"] for x in store.assignments().values()} == {a.id}            # B and C wait: A has nothing to stack on

    work(sched, events, store, a.id, {"a.txt": "a1\n"})
    a1 = store.get_ticket(a.id).reviewed_commit
    ob, _ = work(sched, events, store, b.id, {"b.txt": "b\n"}, who="worker/2")
    oc, _ = work(sched, events, store, c.id, {"c.txt": "c\n"}, who="worker/3")
    A, B, C = (store.get_ticket(x.id) for x in (a, b, c))
    assert B.stacked_on == C.stacked_on == a.id and ob["base"] == oc["base"] == A.branch
    assert head(project, f"{B.branch}~1") == head(project, f"{C.branch}~1") == a1      # each one commit on A's
    assert spice_base(project, B.branch) == spice_base(project, C.branch) == A.branch
    listed = {r["ticket"]: r for r in listing(project, store)}
    assert listed[b.id]["base"] == A.branch and listed[b.id]["parent"] == a.id and listed[b.id]["on_base"]
    verdict(sched, events, store, b.id)
    verdict(sched, events, store, c.id)
    assert store.get_ticket(b.id).state == store.get_ticket(c.id).state == State.APPROVED
    tick(sched, 2)
    assert store.get_ticket(b.id).state == State.APPROVED and not store.gate_runs()  # never landed above an unlanded parent

    verdict(sched, events, store, a.id, approve=False)                                # A reworked: a new commit on A
    work(sched, events, store, a.id, {"a.txt": "a2\n"})
    a2 = store.get_ticket(a.id).reviewed_commit
    until(sched, lambda: ancestor(project, a2, store.get_ticket(b.id).branch) and ancestor(project, a2, store.get_ticket(c.id).branch))
    B, C = store.get_ticket(b.id), store.get_ticket(c.id)
    assert B.reviewed_commit == head(project, B.branch) and head(project, f"{B.branch}~1") == a2
    assert git("show", "--format=", "--name-only", B.reviewed_commit, cwd=project.repo_root).split() == ["b.txt"]
    done = on_bus(events, "stack.restacked")
    assert {e["data"]["ticket"] for e in done} == {b.id, c.id} and {e["source"] for e in done} == {"merge_gate"}
    assert done[0]["data"]["onto"] == A.branch and done[0]["data"]["onto_commit"] == a2
    assert restacks(store) == {"restacked": 2}

    verdict(sched, events, store, a.id)
    until(sched, lambda: all(store.get_ticket(x.id).state == State.DONE for x in (a, b, c)))
    landed = [e["data"]["ticket"] for e in on_bus(events, "gate.landed")]
    assert landed[0] == a.id and set(landed[1:]) == {b.id, c.id}
    main = git("ls-tree", "--name-only", "main", cwd=project.repo_root).split()
    assert {"a.txt", "b.txt", "c.txt"} <= set(main)
    for x in (b, c):
        runs = [r for r in store.gate_runs() if r["ticket"] == x.id]
        assert runs[-1]["status"] == "landed" and any(s["stage"] == "unit" for s in runs[-1]["stages"])   # tested again on the base
        assert store.get_ticket(x.id).stacked_on is None
    onto_base = [e["data"] for e in on_bus(events, "stack.restacked") if e["data"]["onto"] == "main"]
    assert {d["ticket"] for d in onto_base} == {b.id, c.id} and all(d["parent"] == a.id for d in onto_base)
    assert restacks(store) == {"restacked": 4}
    assert not git("branch", "--list", "orchestra/*", cwd=project.repo_root).strip()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=project.repo_root).strip() == "main"    # the root never left its branch


def test_a_restack_conflict_goes_back_to_the_worker_that_did_it(project):
    store, events, sched, ms = setup(project)
    a = store.create_ticket(src="planner", title="a", milestone_id=ms.id)
    b = store.create_ticket(src="planner", title="b", milestone_id=ms.id, depends_on=[a.id])
    work(sched, events, store, a.id, {"shared.txt": "one\n"})
    work(sched, events, store, b.id, {"shared.txt": "one\ntwo from b\n"}, who="worker/2")
    verdict(sched, events, store, b.id)
    b_head = head(project, store.get_ticket(b.id).branch)
    verdict(sched, events, store, a.id, approve=False)
    work(sched, events, store, a.id, {"shared.txt": "one, changed by a\nthree from a\n"})
    until(sched, lambda: store.get_ticket(b.id).state == State.REJECTED)

    B = store.get_ticket(b.id)
    assert B.rejected_by == "merge_gate" and "shared.txt" in B.feedback and B.review_rounds == 0      # not a review round
    assert head(project, B.branch) == b_head                                                          # the branch as it was
    ev = on_bus(events, "stack.conflict", ticket=b.id)
    assert len(ev) == 1 and ev[0]["source"] == "merge_gate" and ev[0]["data"]["files"] == ["shared.txt"]
    back = offer_for(sched, events, store, b.id, "worker")
    assert back["to"] == "worker/2" and "shared.txt" in back["brief"]["feedback"] and back["base"] == store.get_ticket(a.id).branch
    tick(sched, 3)
    assert len(on_bus(events, "stack.conflict", ticket=b.id)) == 1                                    # the same conflict is not tried again
    assert restacks(store) == {"conflict": 1}


def merged(events, t, merge_commit):
    d = {"repository": R, "pull": 7, "login": "owner", "branch": t.branch, "base": "main", "head": t.pull_head, "merge_commit": merge_commit}
    events.publish("github.pr.merged", d, source="person.owner", subject=f"{R}#7", id=f"github:{R}:pr:7:merged:{next(_ids)}")


def squash(project, t, where):
    """The owner's squash merge of ``t``'s branch onto main in the clone ``where``: the new commit."""
    git("merge", "-q", "--squash", t.reviewed_commit, cwd=where)
    git("commit", "-qm", f"{t.id} (#7)", cwd=where)
    return git("rev-parse", "HEAD", cwd=where).strip()


def handed_off_stack(project):
    store, events, sched, ms = setup(project, mode="external")
    a = store.create_ticket(src="planner", title="a", milestone_id=ms.id)
    b = store.create_ticket(src="planner", title="b", milestone_id=ms.id, depends_on=[a.id])
    work(sched, events, store, a.id, {"a.txt": "a\n"})
    work(sched, events, store, b.id, {"b.txt": "b\n"}, who="worker/2")
    verdict(sched, events, store, a.id)
    verdict(sched, events, store, b.id)
    until(sched, lambda: store.get_ticket(b.id).state == State.IN_EXTERNAL_REVIEW == store.get_ticket(a.id).state)
    A = store.get_ticket(a.id)
    hb = on_bus(events, "gate.handed_off", ticket=b.id)[-1]["data"]
    assert hb["base"] == A.branch and hb["parent"] == a.id                           # B's pull request is to be based on A's branch
    assert on_bus(events, "gate.handed_off", ticket=a.id)[-1]["data"]["base"] == "main"
    return store, events, sched, A, store.get_ticket(b.id)


def test_external_a_squash_merge_on_the_base_restacks_the_next_branch_onto_it(project, capsys):
    store, events, sched, A, B = handed_off_stack(project)
    cli(["stack"])
    assert f"{B.branch}  <- {A.branch} ({A.id})" in capsys.readouterr().out
    side = project.repo_root.parent / "owner"
    git("worktree", "add", "-q", "--detach", str(side), "main", cwd=project.repo_root)
    s = squash(project, A, side)
    git("merge", "-q", "--ff-only", s, cwd=project.repo_root)                       # the owner pulled main: the squash is on it
    merged(events, A, s)
    until(sched, lambda: store.get_ticket(B.id).stacked_on is None)
    nb = store.get_ticket(B.id)
    assert store.get_ticket(A.id).state == State.DONE and store.get_ticket(A.id).merged_as == s
    assert head(project, f"{nb.branch}~1") == s and not ancestor(project, A.reviewed_commit, nb.branch)   # A's own commits gone
    assert nb.state == State.IN_EXTERNAL_REVIEW and nb.reviewed_commit == head(project, nb.branch)
    ev = on_bus(events, "stack.restacked", ticket=B.id)[-1]
    assert ev["source"] == "merge_gate" and ev["data"]["onto"] == "main" and ev["data"]["parent"] == A.id
    assert listing(project, store)[0]["base"] == "main" and listing(project, store)[0]["on_base"]


def test_external_a_squash_merge_only_on_the_remote_is_fetched_and_restacked_onto(project):
    remote = project.repo_root.parent / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=project.repo_root.parent)
    git("remote", "add", "origin", str(remote), cwd=project.repo_root)
    git("push", "-q", "origin", "main", cwd=project.repo_root)
    store, events, sched, A, B = handed_off_stack(project)
    git("push", "-q", "origin", A.branch, cwd=project.repo_root)                     # the owner pushed A's branch
    clone = project.repo_root.parent / "github"
    git("clone", "-q", str(remote), str(clone), cwd=project.repo_root.parent)
    git("config", "user.email", "t@t", cwd=clone)
    git("config", "user.name", "t", cwd=clone)
    git("fetch", "-q", "origin", A.branch, cwd=clone)
    main_before = head(project, "main")
    s = squash(project, A, clone)
    git("push", "-q", "origin", "main", cwd=clone)                                   # merged on the other side; nothing pulled here
    merged(events, A, s)
    until(sched, lambda: store.get_ticket(B.id).stacked_on is None)
    nb = store.get_ticket(B.id)
    assert head(project, f"{nb.branch}~1") == s and head(project, "main") == main_before    # the base branch untouched
    assert git("show", "--format=", "--name-only", nb.reviewed_commit, cwd=project.repo_root).split() == ["b.txt"]


def test_stacking_with_the_worktree_pool(project):
    """A chain of three with reused worktrees: each turn is handed a worktree of the pool reset to
    its stacked branch, with the work below it there; a new commit at the bottom restacks both
    branches above it in one tick (parents first, in the stack worktree, since no pool worktree is
    a ticket's own); they land bottom first, within the pool's size."""
    store, events, sched, ms = setup(project, pool=True)
    a = store.create_ticket(src="planner", title="a", milestone_id=ms.id)
    b = store.create_ticket(src="planner", title="b", milestone_id=ms.id, depends_on=[a.id])
    c = store.create_ticket(src="planner", title="c", milestone_id=ms.id, depends_on=[b.id])
    oa, _ = work(sched, events, store, a.id, {"a.txt": "a1\n"})
    ob, _ = work(sched, events, store, b.id, {"b.txt": "b\n"}, who="worker/2")
    oc, _ = work(sched, events, store, c.id, {"c.txt": "c\n"}, who="worker/1")
    A, B, C = (store.get_ticket(x.id) for x in (a, b, c))
    assert (B.stacked_on, C.stacked_on) == (a.id, b.id) and (ob["base"], oc["base"]) == (A.branch, B.branch)
    for o in (oa, ob, oc):
        assert o["workspace"].startswith(str(project.pool_dir))
    assert ancestor(project, A.reviewed_commit, C.reviewed_commit) and ancestor(project, B.reviewed_commit, C.reviewed_commit)
    verdict(sched, events, store, b.id)
    verdict(sched, events, store, c.id)
    verdict(sched, events, store, a.id, approve=False)
    work(sched, events, store, a.id, {"a.txt": "a2\n"})
    a2 = store.get_ticket(a.id).reviewed_commit
    until(sched, lambda: ancestor(project, a2, store.get_ticket(c.id).branch))
    B, C = store.get_ticket(b.id), store.get_ticket(c.id)
    assert ancestor(project, a2, B.branch) and head(project, f"{C.branch}~1") == head(project, B.branch)
    assert [e["data"]["ticket"] for e in on_bus(events, "stack.restacked")] == [b.id, c.id]      # the bottom first, in one tick
    verdict(sched, events, store, a.id)
    until(sched, lambda: all(store.get_ticket(x.id).state == State.DONE for x in (a, b, c)))
    assert [e["data"]["ticket"] for e in on_bus(events, "gate.landed")] == [a.id, b.id, c.id]
    assert set(git("ls-tree", "--name-only", "main", cwd=project.repo_root).split()) >= {"a.txt", "b.txt", "c.txt"}
    assert git("show", "main:a.txt", cwd=project.repo_root) == "a2\n"
    assert len(store.pool()) <= 4 and all(x["free"] for x in store.pool().values())
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=project.repo_root).strip() == "main"
