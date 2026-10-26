import subprocess
import time

import pytest

from tasking.config import init_project
from tasking.roles import RepoRoleStore, RoleDef, RoleError, all_roles, due_at


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    p = init_project(repo)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "orchestra"], cwd=repo, check=True)
    return p


def test_repo_store_saves_commits_and_deletes(project):
    rs = RepoRoleStore(project)
    r = RoleDef(name="product_owner", description="keeps stories current", tools=["tickets", "escalate"],
                wake_on=["ticket_transition:DONE", "milestone_closed"], every="1d", at="09:00")
    rs.save(r, "# You keep the product honest\n\nWatch landings and keep user stories current.")
    assert (project.repo_root / ".orchestra" / "roles" / "product_owner.json").exists()
    assert rs.last_commit_note.startswith("committed")
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=project.repo_root, capture_output=True, text=True).stdout
    assert "role product_owner" in log
    assert rs.get("product_owner").every == "1d" and "product honest" in rs.prompt("product_owner")
    assert "product_owner" in all_roles(project) and all_roles(project)["coordinator"].builtin
    rs.delete("product_owner")
    with pytest.raises(RoleError):
        rs.get("product_owner")


def test_validation():
    with pytest.raises(RoleError):
        RoleDef(name="Bad Name").validate()
    with pytest.raises(RoleError):
        RoleDef(name="x_role", tools=["nope"]).validate()
    with pytest.raises(RoleError):
        RoleDef(name="x_role", wake_on=["not_an_event"]).validate()
    RoleDef(name="x_role", wake_on=["ticket_transition:BLOCKED"], every="30m").validate()


def test_event_matching_and_daily_due():
    r = RoleDef(name="w", wake_on=["ticket_transition:DONE", "gate_landed"])
    assert r.matches_event({"type": "ticket_transition", "to": "DONE"})
    assert not r.matches_event({"type": "ticket_transition", "to": "OPEN"})
    assert r.matches_event({"type": "gate_landed"})
    noon_today = time.mktime(time.localtime()[:3] + (12, 0, 0, 0, 0, -1))
    assert due_at("12:00", None, noon_today + 60)
    assert not due_at("12:00", noon_today + 30, noon_today + 60)   # already fired today
    assert not due_at("12:00", None, noon_today - 60)              # not yet


def test_get_role_explains_what_the_role_is(project):
    """The Agents page shows what makes a role that role: its effective prompt, where it
    comes from, the concrete tools behind its groups, and its schedule."""
    from tasking import api
    from tasking.roles import RoleDef, role_store
    from tasking.store import LocalStore

    store = LocalStore(project)
    r = api.handle(project, store, "get_role", {"name": "coordinator"})
    assert r["role"]["builtin"] and "Coordinator" in r["prompt"] and r["prompt_source"].endswith("prompts/coordinator.md")
    assert "create_ticket" in r["tool_names"]["tickets"] and "set_capacity" in r["tool_names"]["capacity"]
    assert r["agents"] == ["coordinator"]
    role_store(project).save(RoleDef(name="product_owner", description="Keeps the backlog full", tools=["tickets", "escalate"],
                                     wake_on=["ticket_transition:DONE"], every="45m"), "Be opinionated about {base_branch}.")
    r = api.handle(project, store, "get_role", {"name": "product_owner"})
    assert not r["role"]["builtin"] and r["prompt"].strip() == "Be opinionated about main."   # the job description, placeholders resolved
    a = api.handle(project, store, "get_agent", {"name": "product_owner"})                 # a Stage 1 role is its own agent
    assert a["agent"]["source"] == "role" and a["agent"]["every_seconds"] == 45 * 60 and a["agent"]["kind"] == "scheduled"
    assert a["prompt"].startswith("# You are the `product_owner` agent of Orchestra")     # Orchestra's preamble
    assert "## Your role: product_owner" in a["prompt"] and "Be opinionated about main." in a["prompt"]
    assert r["prompt_source"].endswith(".orchestra/roles/product_owner.md")
    assert r["tool_names"] == {"tickets": r["tool_names"]["tickets"], "escalate": ["escalate_to_supervisor"]}
