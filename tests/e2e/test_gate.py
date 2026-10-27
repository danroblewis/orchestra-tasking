"""The merge gate's decisions, each driven directly with a real git repository.

The gate is the only thing that lands code, so every outcome it can reach is exercised:
landed, rejected (the merged result fails tests), base_broken (the base itself fails),
conflict (the branch does not merge), a build whose artifact is missing, and the full
build + start + acceptance stage run. The ticket state, notes, feedback, pipeline flag,
and attention each outcome must produce are asserted, not just the run status."""

from pathlib import Path

import pytest
from conftest import UNIT, asked, git

from tasking.config import Config
from tasking.events import Events
from tasking.gate import run_gate
from tasking.model import State
from tasking.store import LocalStore


@pytest.fixture
def env(project):
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    ms = store.create_milestone(name="m")
    return project, store, events, ms, project.repo_root


def set_config(project, repo, toml: str):
    (repo / "orchestra.toml").write_text(toml)
    project.config = Config.from_toml(toml)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "config", cwd=repo)


def merging_ticket(store, ms, repo, title, mutate, base="main"):
    """A ticket in MERGING with a branch off `base` whose content `mutate` produced,
    walked through the same transitions the scheduler uses."""
    t = store.create_ticket(title=title, milestone_id=ms.id)
    branch = f"orchestra/{t.id}"
    wt = repo.parent / f"wt-{t.id}"
    git("worktree", "add", "-q", "-b", branch, str(wt), base, cwd=repo)
    mutate(wt)
    git("add", "-A", cwd=wt)
    git("commit", "-qm", f"{t.id}: work", cwd=wt)
    head = git("rev-parse", "HEAD", cwd=wt).strip()
    git("worktree", "remove", "--force", str(wt), cwd=repo)
    t = store.transition(t.id, t.version, State.IN_PROGRESS, branch=branch)
    t = store.transition(t.id, t.version, State.READY_FOR_REVIEW, reviewed_commit=head)
    t = store.transition(t.id, t.version, State.IN_REVIEW)
    t = store.transition(t.id, t.version, State.APPROVED)
    return store.transition(t.id, t.version, State.MERGING)


def event_types(events):
    evs, _ = events.read(0, limit=10000)
    return [e["type"] for e in evs]


def test_landed(env):
    project, store, events, ms, repo = env
    t = merging_ticket(store, ms, repo, "good", lambda wt: (wt / "NEW.md").write_text("ok\n"))
    run = run_gate(project, store, events, t.id)
    assert run["status"] == "landed" and run["ticket"] == t.id and run["conflict"] is False
    assert store.get_ticket(t.id).state == State.DONE
    assert "NEW.md" in git("ls-tree", "--name-only", "main", cwd=repo)
    assert git("log", "-1", "--format=%s", "main", cwd=repo).startswith(f"Merge {t.id}")
    assert [s["name"] for s in run["stages"]] == ["unit"] and run["stages"][0]["status"] == "passed"
    assert not (project.gate_dir / run["id"]).exists()
    assert not git("branch", "--list", "orchestra/*", cwd=repo).strip()
    assert "gate.landed" in event_types(events)


def test_failing_tests_reject_the_ticket(env):
    project, store, events, ms, repo = env
    before = git("rev-parse", "main", cwd=repo)
    t = merging_ticket(store, ms, repo, "breaks tests", lambda wt: (wt / "BROKEN").write_text("x"))
    run = run_gate(project, store, events, t.id)
    assert run["status"] == "rejected" and run["ticket"] == t.id and run["conflict"] is False
    t = store.get_ticket(t.id)
    assert t.state == State.REJECTED and t.rejected_by == "merge_gate" and t.review_rounds == 1
    assert t.gate_rejections == 1 and t.reviewer_rejections == 0
    assert "unit" in t.feedback and run["id"] in t.feedback
    notes = store.notes(t.id)
    assert notes and notes[-1]["kind"] == "gate" and notes[-1]["author"] == "merge_gate"
    assert git("rev-parse", "main", cwd=repo) == before            # nothing landed
    assert run["base_check"] and all(s["status"] == "passed" for s in run["base_check"])  # base was fine
    assert store.pipeline()["paused"] is False
    assert not (project.gate_dir / run["id"]).exists()
    assert git("branch", "--list", f"orchestra/{t.id}", cwd=repo).strip()  # the worker's branch survives
    assert "gate.rejected" in event_types(events)


def test_broken_base_pauses_pipeline_and_keeps_ticket_approved(env):
    project, store, events, ms, repo = env
    (repo / "BROKEN").write_text("x")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "break main", cwd=repo)
    t = merging_ticket(store, ms, repo, "innocent", lambda wt: (wt / "NEW.md").write_text("ok\n"))
    run = run_gate(project, store, events, t.id)
    assert run["status"] == "base_broken"
    t = store.get_ticket(t.id)
    assert t.state == State.APPROVED and t.review_rounds == 0 and t.rejected_by is None
    assert run["base_check"] and any(s["status"] == "failed" for s in run["base_check"])
    p = store.pipeline()
    assert p["paused"] is True and run["id"] in p["reason"]
    wake = asked(events, "supervisor")
    assert wake and "broken" in wake[0]["reason"]
    assert "gate.base_broken" in event_types(events)
    assert not (project.gate_dir / f"{run['id']}-base").exists()


def test_an_unexpected_error_pauses_the_pipeline_and_keeps_the_ticket_approved(env, monkeypatch):
    """Whatever fails inside a run, the ticket never stays MERGING with no holder."""
    from tasking import gate
    project, store, events, ms, repo = env
    monkeypatch.setattr(gate, "run_specs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    t = merging_ticket(store, ms, repo, "unlucky", lambda wt: (wt / "NEW.md").write_text("ok\n"))
    run = run_gate(project, store, events, t.id)
    assert run["status"] == "error" and "boom" in run["summary"] and run["finished_at"]
    assert store.get_ticket(t.id).state == State.APPROVED
    assert store.pipeline()["paused"] and run["id"] in store.pipeline()["reason"]
    assert "gate.error" in event_types(events) and asked(events, "supervisor")
    assert not (project.gate_dir / run["id"]).exists()


def test_conflict_goes_back_to_worker_without_a_review_round(env):
    project, store, events, ms, repo = env
    original = git("rev-parse", "main", cwd=repo).strip()
    a = merging_ticket(store, ms, repo, "A", lambda wt: (wt / "README.md").write_text("# gate\nline from A\n"))
    assert run_gate(project, store, events, a.id)["status"] == "landed"
    b = merging_ticket(store, ms, repo, "B", lambda wt: (wt / "README.md").write_text("# gate\nline from B\n"), base=original)
    run = run_gate(project, store, events, b.id)
    assert run["status"] == "conflict" and run["ticket"] == b.id and run["conflict"] is True
    b = store.get_ticket(b.id)
    assert b.state == State.REJECTED and b.rejected_by == "merge_gate"
    assert b.review_rounds == 0                       # routine, not a review failure
    assert b.gate_rejections == 1                     # but still a re-engagement the human can see
    assert "conflicts with `main`" in b.feedback
    assert git("show", "main:README.md", cwd=repo) == "# gate\nline from A\n"
    assert "gate.conflict" in event_types(events)


def test_missing_build_artifact_is_a_base_problem(env):
    """A build that produces no artifact fails on the base too, so the gate blames the
    configuration (pipeline paused, the supervisor role told), not the ticket."""
    project, store, events, ms, repo = env
    set_config(project, repo, f'[build]\ncommand = "true"\nartifact = "dist/*.whl"\n[[gate.unit]]\nname = "unit"\ncommand = "{UNIT}"\n')
    t = merging_ticket(store, ms, repo, "no artifact", lambda wt: (wt / "NEW.md").write_text("ok\n"))
    run = run_gate(project, store, events, t.id)
    assert run["status"] == "base_broken"
    assert run["stages"][-1] == {"stage": "build", "name": "artifact", "status": "failed", "tail": "no artifact matched dist/*.whl"}
    assert "unit" not in [s["name"] for s in run["stages"]]  # later stages did not run
    assert store.get_ticket(t.id).state == State.APPROVED and store.pipeline()["paused"]
    assert asked(events, "supervisor")


def test_build_start_and_acceptance_stages(env):
    project, store, events, ms, repo = env
    set_config(project, repo, (
        '[build]\ncommand = "mkdir -p dist && echo hi > dist/app.txt"\nartifact = "dist/*.txt"\n'
        '[acceptance]\nstart = "cd $(dirname {artifact}) && python3 -m http.server {port} --bind 127.0.0.1"\n'
        'ready = "{url}/"\nready_timeout = "20s"\n'
        f'[[gate.unit]]\nname = "unit"\ncommand = "{UNIT}"\n'
        '[[gate.acceptance]]\nname = "acc"\ncommand = "curl -sf {url}/app.txt | grep -q hi"\n'))
    t = merging_ticket(store, ms, repo, "full", lambda wt: (wt / "NEW.md").write_text("ok\n"))
    run = run_gate(project, store, events, t.id)
    assert run["status"] == "landed", run
    assert [(s["stage"], s["status"]) for s in run["stages"]] == [("build", "passed"), ("unit", "passed"), ("acceptance", "passed")]
    assert Path(run["artifact"]).read_text() == "hi\n"
    assert Path(run["artifact"]).is_relative_to(project.state_dir)
    assert (project.logs_dir / run["id"] / "target.log").exists()
    assert store.get_ticket(t.id).state == State.DONE


def test_acceptance_failure_rejects_and_stops_target(env):
    project, store, events, ms, repo = env
    set_config(project, repo, (
        '[build]\ncommand = "mkdir -p dist && echo hi > dist/app.txt"\nartifact = "dist/*.txt"\n'
        '[acceptance]\nstart = "cd $(dirname {artifact}) && python3 -m http.server {port} --bind 127.0.0.1"\n'
        'ready = "{url}/"\nready_timeout = "20s"\n'
        f'[[gate.unit]]\nname = "unit"\ncommand = "{UNIT}"\n'
        '[[gate.acceptance]]\nname = "acc"\ncommand = "curl -sf {url}/missing.txt"\n'))
    t = merging_ticket(store, ms, repo, "acc fails", lambda wt: (wt / "NEW.md").write_text("ok\n"))
    run = run_gate(project, store, events, t.id)
    assert run["status"] == "base_broken"   # the acceptance test fails on the base too: not this ticket's fault
    assert store.get_ticket(t.id).state == State.APPROVED and store.pipeline()["paused"]
    import psutil
    assert not [p for p in psutil.process_iter(["cmdline"]) if p.info["cmdline"] and "http.server" in " ".join(p.info["cmdline"])
                and str(project.state_dir) in " ".join(p.info["cmdline"])]


def test_a_landed_orchestra_toml_change_the_root_cannot_see_is_reported(env):
    """The root has another branch checked out: landing moves only the base's ref, so the root's
    orchestra.toml (which Orchestra's launcher reads) stays as it was, and the coordinator is told."""
    project, store, events, ms, repo = env
    git("checkout", "-q", "-b", "dev", cwd=repo)
    t = merging_ticket(store, ms, repo, "a collector",
                       lambda wt: (wt / "orchestra.toml").write_text((wt / "orchestra.toml").read_text() + '\n[[machines]]\nname = "the-collector"\ncommand = "true"\n'))
    run = run_gate(project, store, events, t.id)
    assert run["status"] == "landed" and "the-collector" in git("show", "main:orchestra.toml", cwd=repo)
    assert "the-collector" not in (repo / "orchestra.toml").read_text()                     # the root did not follow
    [a] = asked(events, "coordinator")
    assert a["ticket"] == t.id and "the repository root is on dev, not main" in a["reason"]
    assert "takes effect only when the root is updated" in run["summary"]


def test_a_landing_without_orchestra_toml_off_the_base_says_nothing(env):
    project, store, events, ms, repo = env
    git("checkout", "-q", "-b", "dev", cwd=repo)
    t = merging_ticket(store, ms, repo, "docs", lambda wt: (wt / "NEW.md").write_text("ok\n"))
    assert run_gate(project, store, events, t.id)["status"] == "landed"
    assert asked(events, "coordinator") == []


def test_untracked_files_in_the_way_of_a_landing_are_named(env):
    """An untracked file in the root at a path the landed commit adds: the fast-forward would fail;
    the gate error names the file, so the attention says what to move."""
    project, store, events, ms, repo = env
    t = merging_ticket(store, ms, repo, "a sampler", lambda wt: ((wt / "scripts").mkdir(), (wt / "scripts" / "sampler.py").write_text("x = 1\n")))
    (repo / "scripts").mkdir()
    (repo / "scripts" / "sampler.py").write_text("an earlier copy\n")
    before = git("rev-parse", "main", cwd=repo)
    run = run_gate(project, store, events, t.id)
    assert run["status"] == "error" and "scripts/sampler.py" in run["summary"]
    assert git("rev-parse", "main", cwd=repo) == before and store.get_ticket(t.id).state == State.APPROVED
    [a] = asked(events, "supervisor")
    assert "untracked files in the repository root would be overwritten" in a["reason"] and "scripts/sampler.py" in a["reason"]
