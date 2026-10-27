"""Reused worktrees (``[tasks] reuse_worktrees``), end to end: the real scheduler, store, git and
gate with the fake machine, which looks at each worktree it is handed before working in it. The
pool never grows past its size, every turn starts clean on the ticket's branch, ignored files
survive a reset unless the stricter setting is on, and a worktree a crash left dirty is put right."""

import pytest
from conftest import git, run_scheduler

from tasking.assignments import ACCEPTED, ASSIGNED
from tasking.config import Config
from tasking.events import Events
from tasking.model import State
from tasking.scheduler import Scheduler
from tasking.store import LocalStore


@pytest.fixture
def pool_env(project):
    """A code-mode project whose `.gitignore` ignores `cache/`, with reused worktrees on."""
    def make(pool: int, clean_ignored: bool = False, workers: int = 2):
        repo = project.repo_root
        toml = repo / "orchestra.toml"
        text = toml.read_text().replace('work = "tasks"', 'work = "code"').replace("workers = 2", f"workers = {workers}")
        text = text.replace("[tasks]\n", f"[tasks]\nreuse_worktrees = true\nworktree_pool = {pool}\n"
                                        f"worktree_clean_ignored = {'true' if clean_ignored else 'false'}\n")
        toml.write_text(text)
        (repo / ".gitignore").write_text("cache/\n")
        project.reload_config()
        git("add", "-A", cwd=repo)
        git("commit", "-qm", "code mode, reused worktrees", cwd=repo)
        events = Events.for_project(project, source="task_scheduler")
        store = LocalStore(project, events)
        return project, store, events, store.create_milestone(src="planner", name="m"), repo
    return make


def looked(a, path):
    """What a machine finds in the worktree it is handed."""
    run = lambda *args: git(*args, cwd=path).strip()
    return {"role": a["role"], "ticket": a["ticket"], "path": a["workspace"], "branch": a["branch"],
            "head": run("rev-parse", "--abbrev-ref", "HEAD"),
            "status": run("status", "--porcelain"),
            "at_base": run("rev-parse", "HEAD") == run("rev-parse", "main"),
            "litter": (path / "LITTER.txt").exists(), "cache": sorted(p.name for p in (path / "cache").glob("*")),
            "work": (path / "WORK.md").read_text() if (path / "WORK.md").exists() else ""}


def test_five_tickets_in_a_row_use_at_most_two_worktrees_each_clean_on_its_branch(pool_env):
    project, store, events, ms, repo = pool_env(pool=2)
    ts = []
    for i in range(5):                                       # in a row: each depends on the one before
        ts.append(store.create_ticket(src="planner", title=f"ticket {i}", milestone_id=ms.id, depends_on=[ts[-1].id] if ts else []))
    seen = []
    run_scheduler(project, store, events, look=lambda a, p: seen.append(looked(a, p)))
    assert all(store.get_ticket(t.id).state == State.DONE for t in ts)
    assert {s["path"] for s in seen} <= {str(project.pool_dir / "1"), str(project.pool_dir / "2")}
    assert len({s["ticket"] for s in seen}) == 5 and len(seen) == 10              # a worker and a reviewer each
    for s in seen:
        assert s["head"] == s["branch"] == store.get_ticket(s["ticket"]).branch and s["status"] == "", s
        if s["role"] == "worker":
            assert s["at_base"], s                                                # a new branch from the base as it stands
    assert len(store.pool()) <= 2 and all(p["free"] for p in store.pool().values())
    assert len(git("worktree", "list", "--porcelain", cwd=repo).split("\n\n")) <= 3   # the root and two
    assert not list(project.gate_dir.glob("*")) and not list(project.worktrees_dir.glob("*"))
    assert not git("branch", "--list", "orchestra/*", cwd=repo).strip()          # every branch and gate branch deleted
    assert all(r["status"] == "landed" for r in store.gate_runs())


@pytest.mark.parametrize("clean_ignored", [False, True])
def test_untracked_files_never_leak_and_ignored_ones_stay_unless_cleaned(pool_env, clean_ignored):
    """One worktree for two tickets: the first ticket's reviewer leaves an untracked file and an
    ignored cache; the second ticket's worker finds the cache (warm) but not the file, or neither
    with ``worktree_clean_ignored``. The second waits for the worktree, and says so."""
    project, store, events, ms, _repo = pool_env(pool=1, clean_ignored=clean_ignored)
    t1 = store.create_ticket(src="planner", title="first", milestone_id=ms.id, priority=1)
    t2 = store.create_ticket(src="planner", title="second", milestone_id=ms.id, priority=2)
    seen, waited = [], []
    record = store.set_pool_waiting
    store.set_pool_waiting = lambda n: (waited.append(n), record(n))[1]            # what the record is told waits
    run_scheduler(project, store, events, litter=True, look=lambda a, p: seen.append(looked(a, p)))
    assert store.get_ticket(t1.id).state == store.get_ticket(t2.id).state == State.DONE
    assert {s["path"] for s in seen} == {str(project.pool_dir / "1")}
    second = next(s for s in seen if s["ticket"] == t2.id and s["role"] == "worker")
    assert not second["litter"] and second["status"] == "" and second["head"] == store.get_ticket(t2.id).branch
    assert second["cache"] == ([] if clean_ignored else [t1.id])
    log = (project.logs_dir / "scheduler.log").read_text()
    assert f"{t2.id}: the worktree pool is full (1 in use); the worker waits for a free one" in log
    assert max(waited) >= 1 and waited[-1] == 0                                     # tasks_worktrees{state="waiting"}, served from the record
    assert 'tasks_worktrees{state="waiting"} 0' in store.metrics_text() and 'tasks_worktrees{state="free"} 1' in store.metrics_text()


def test_a_worktree_a_crash_left_dirty_is_reset_and_the_ticket_gets_it_back(pool_env):
    """A worker holds a ticket, changes things and the scheduler dies; the next scheduler releases
    the assignment, and the ticket's next turn has the same worktree, reset: tracked changes
    discarded, untracked and staged files gone, the ignored cache kept."""
    project, store, events, ms, _repo = pool_env(pool=2)
    t = store.create_ticket(src="planner", title="interrupted", milestone_id=ms.id)
    first = Scheduler(project, store, events)
    first.recover()
    first.tick()
    offer = [e for e in events.after(0)[0] if e["type"] == ASSIGNED][-1]
    events.publish(ACCEPTED, {"assignment": offer["assignment"], "ticket": t.id, "role": "worker"}, source="worker/1", subject=t.id)
    first.tick()
    assert store.get_ticket(t.id).state == State.IN_PROGRESS
    wt = project.pool_dir / "1"
    assert offer["workspace"] == str(wt)
    (wt / "README.md").write_text("half done\n")
    (wt / "stray.txt").write_text("untracked\n")
    (wt / "staged.txt").write_text("staged\n")
    git("add", "staged.txt", cwd=wt)
    (wt / "cache").mkdir()
    (wt / "cache" / "built").write_text("warm\n")
    # the first scheduler is gone without shutting down: a crash
    seen = []
    run_scheduler(project, store, events, look=lambda a, p: seen.append(looked(a, p)))
    t = store.get_ticket(t.id)
    assert t.state == State.DONE and t.worker_turns == 2
    again = next(s for s in seen if s["role"] == "worker")
    assert again["path"] == str(wt) and again["status"] == "" and again["head"] == t.branch and again["cache"] == ["built"]
    assert not any(n["text"].endswith("the workspace is as it was left") for n in store.notes(t.id))


def test_a_rejected_ticket_gets_its_worktree_back_with_its_work(pool_env):
    project, store, events, ms, _repo = pool_env(pool=2)
    t = store.create_ticket(src="planner", title="twice", milestone_id=ms.id)
    seen = []
    run_scheduler(project, store, events, reject_first=True, look=lambda a, p: seen.append(looked(a, p)))
    t = store.get_ticket(t.id)
    assert t.state == State.DONE and t.worker_turns == 2
    first, second = [s for s in seen if s["role"] == "worker"]
    assert first["path"] == second["path"] and second["head"] == t.branch
    assert second["work"] == f"work on {t.id}\n" and second["status"] == ""        # its first turn's commit, nothing else


def test_the_gate_works_in_the_pool_and_checks_the_base_in_the_same_worktree(pool_env):
    """A gate run that fails checks the base alone; with reused worktrees both happen in the one
    worktree the gate holds, and nothing is made under the gate's own directory."""
    project, store, events, ms, repo = pool_env(pool=1)
    t = store.create_ticket(src="planner", title="breaks the build first", milestone_id=ms.id)
    run_scheduler(project, store, events, break_first=True)
    assert store.get_ticket(t.id).state == State.DONE
    runs = store.gate_runs()
    assert [r["status"] for r in runs] == ["rejected", "landed"] and runs[0]["base_check"]
    assert list(store.pool()) == ["1"] and not list(project.gate_dir.glob("*"))
    assert not git("branch", "--list", "orchestra/*", cwd=repo).strip()


def test_a_gate_run_a_crash_interrupted_leaves_no_hold_and_no_branch(pool_env):
    project, store, events, _ms, repo = pool_env(pool=1)
    sched = Scheduler(project, store, events)
    path = sched.pool.claim("gate:T-1", "T-1")
    sched.pool.reset(path, "orchestra/gate-abc")
    (path / "half-merged").write_text("x\n")
    assert store.claim_slot("T-2.worker.x", "T-2", size=1) is None          # the gate holds the only one
    Scheduler(project, store, events).recover()                             # the next scheduler
    assert store.pool()["1"]["free"] and not git("branch", "--list", "orchestra/gate-*", cwd=repo).strip()


def test_the_pool_prefers_the_tickets_own_worktree_then_the_longest_unused_and_never_grows_past_its_size(project):
    store = LocalStore(project, Events.for_project(project, source="task_scheduler"))
    for aid in ("A", "B"):
        store.put_assignment({"id": aid})
    assert [store.claim_slot(aid, tid, size=2) for aid, tid in (("A", "T-1"), ("B", "T-2"))] == [
        ({"name": "1", "path": str(project.pool_dir / "1"), "holder": "A", "ticket": "T-1", "since": store.pool()["1"]["since"]}, True),
        ({"name": "2", "path": str(project.pool_dir / "2"), "holder": "B", "ticket": "T-2", "since": store.pool()["2"]["since"]}, True)]
    assert store.claim_slot("C", "T-3", size=2) is None                    # both held: none made past the size
    store.drop_assignment("A")                                             # their turns end: free, nothing else to do
    store.drop_assignment("B")
    assert store.claim_slot("C", "T-2", size=2, prefer="T-2")[0]["name"] == "2"   # the ticket's own
    assert store.claim_slot("D", "T-4", size=2)[0]["name"] == "1"          # the longest unused
    held, _ = store.claim_slot("gate:T-9", "T-9", size=2)                   # a gate's hold is not an assignment's:
    assert store.pool()[held["name"]]["free"] is False                     # it lasts until released
    store.release_gate_slots()
    assert store.pool()[held["name"]]["free"] is True


def test_the_settings_and_their_defaults():
    c = Config.from_toml('[tasks]\nworkers = 3\nreviewers = 2\n')
    assert (c.reuse_worktrees, c.worktree_pool, c.worktree_clean_ignored) == (False, 6, False)
    c = Config.from_toml('[tasks]\nreuse_worktrees = true\nworktree_pool = 2\nworktree_clean_ignored = true\n')
    assert (c.reuse_worktrees, c.worktree_pool, c.worktree_clean_ignored) == (True, 2, True)
