"""Roles, agents, and teams as three things (docs/proposals/teams-and-roles.md, Stage 2)."""

import json
import subprocess

import pytest

from tasking import api
from tasking.config import init_project
from tasking.org import (
    AgentDef,
    OrgError,
    RepoOrgStore,
    TeamDef,
    all_agents,
    all_teams,
    effective_tools,
    may_message,
)
from tasking.roles import RoleDef, role_store
from tasking.store import LocalStore, StoreError


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


def git_log(project):
    return subprocess.run(["git", "log", "--format=%s"], cwd=project.repo_root, capture_output=True, text=True, check=True).stdout


def test_builtins_are_agents_in_teams(project):
    ags, teams = all_agents(project), all_teams(project)
    assert ags["coordinator"].team == "engineering" and ags["coordinator"].kind == "scheduled" and ags["coordinator"].builtin
    assert ags["worker"].kind == "ad_hoc" and not ags["worker"].resident
    assert ags["supervisor"].team is None
    assert teams["engineering"].lead == "coordinator" and teams["engineering"].agents == ["coordinator", "worker", "reviewer"]
    assert "pipeline" not in teams                                   # no Pipeline Manager: the Supervisor handles the gate and staging


def test_validation():
    with pytest.raises(OrgError):
        AgentDef(name="x", roles=[]).validate()                                   # needs a role
    with pytest.raises(OrgError):
        AgentDef(name="x_a", roles=["r"], kind="ad_hoc", every="1h").validate()  # ad hoc has no schedule
    with pytest.raises(OrgError):
        AgentDef(name="x_a", roles=["r"], kind="scheduled").validate()           # scheduled needs one
    with pytest.raises(OrgError, match="always up"):
        AgentDef(name="x_a", roles=["r"], kind="continuous", session="ephemeral").validate()   # a contradiction
    with pytest.raises(OrgError, match="fresh session"):
        AgentDef(name="x_a", roles=["r"], kind="ad_hoc", session="resident").validate()
    AgentDef(name="x_a", roles=["r"], kind="ad_hoc", session="ephemeral", wake_on=["gate_conflict"]).validate()  # on events, fresh each time
    AgentDef(name="x_a", roles=["r"], kind="scheduled", every="1d", session="ephemeral").validate()
    with pytest.raises(OrgError):
        TeamDef(name="Bad", lead="a").validate()
    with pytest.raises(OrgError):
        TeamDef(name="t", lead="a", members=["a"]).validate()                    # lead is not a member
    with pytest.raises(OrgError):
        TeamDef(name="t", lead="a", access=[{"path": "../x", "access": "read"}]).validate()
    with pytest.raises(OrgError):
        TeamDef(name="t", lead="a", access=[{"tools": ["nope"]}]).validate()
    t = TeamDef(name="product/qa", lead="a", access=[{"tools": ["tickets"]}, {"path": "tests", "access": "read"}])
    t.validate()
    assert t.parent == "product" and t.space == ".orchestra/spaces/product/qa" and t.tool_groups == ["tickets"]


def test_store_round_trip_nesting_space_and_commits(project):
    os_ = RepoOrgStore(project)
    role_store(project).save(RoleDef(name="qa_engineer", description="tests things", tools=["tickets"]), "# You test things\n\nThoroughly, in a browser.")
    os_.save_agent(AgentDef(name="qa_lead", roles=["qa_engineer"], team="product/qa", kind="scheduled", every="1d"))
    with pytest.raises(OrgError):
        os_.save_team(TeamDef(name="product/qa", lead="qa_lead"), "# QA\n1. x")      # parent must exist
    os_.save_team(TeamDef(name="product", lead="po"), "# Product\n\n1. The backlog is never empty.")
    os_.save_team(TeamDef(name="product/qa", lead="qa_lead", access=[{"tools": ["tickets", "events"]}]), "# QA\n\n1. Every milestone has an e2e test.")
    assert os_.get_team("product/qa").parent == "product"
    assert "e2e test" in os_.responsibilities("product/qa")
    assert (project.repo_root / ".orchestra" / "teams" / "product" / "qa.json").exists()
    assert (project.repo_root / ".orchestra" / "spaces" / "product" / "qa" / "tools").is_dir()
    gi = (project.repo_root / ".orchestra" / ".gitignore").read_text()
    assert "spaces/product/" in gi and "spaces/product/qa/" in gi             # untracked by default
    assert "team product/qa" in git_log(project) and os_.last_commit_note.startswith("committed")
    os_.save_team(TeamDef(name="product", lead="po", tracked=True), "# Product\n\n1. x")
    assert "spaces/product/\n" not in (project.repo_root / ".orchestra" / ".gitignore").read_text()
    with pytest.raises(OrgError):
        os_.delete_team("product")                                                  # has a sub-team
    assert [a.name for a in os_.list_agents()] == ["qa_lead"]
    assert all_teams(project)["product/qa"].lead == "qa_lead"


def test_stage1_roles_become_implicit_agents(project):
    role_store(project).save(RoleDef(name="product_owner", description="keeps the backlog full", tools=["tickets", "escalate"],
                                     wake_on=["ticket_transition:DONE"], every="45m"), "# You own the product\n\nKeep the backlog full.")
    a = all_agents(project)["product_owner"]
    assert a.source == "role" and a.kind == "scheduled" and a.every == "45m" and a.wake_on == ["ticket_transition:DONE"]
    assert a.resident and a.team is None
    RepoOrgStore(project).save_agent(AgentDef(name="product_owner", roles=["product_owner"], kind="continuous", wake_on=["milestone_closed"]))
    a = all_agents(project)["product_owner"]
    assert a.source == "agent" and a.kind == "continuous" and a.resident          # an explicit record wins
    # a record from before the kinds were pinned down reads as what it is
    (project.repo_root / ".orchestra" / "agents" / "old.json").write_text(
        json.dumps({"name": "old", "roles": ["product_owner"], "kind": "continuous", "session": "ephemeral", "wake_on": ["gate_conflict"]}))
    old = all_agents(project)["old"]
    assert old.kind == "ad_hoc" and not old.resident and old.matches_event({"type": "gate_conflict"})


def test_team_record_decides_membership(project):
    """A Stage 1 role record (no team field) named by a team belongs to it; an agent whose
    own record names the team belongs to it even before the team lists it."""
    from tasking.org import members_of
    rs, os_ = role_store(project), RepoOrgStore(project)
    rs.save(RoleDef(name="product_owner", tools=["tickets"], every="45m"), "# You own\n\nThe product, with opinions.")
    rs.save(RoleDef(name="owner", tools=["tickets"]), "# You own\n\nThings.")
    os_.save_agent(AgentDef(name="writer", roles=["owner"], team="product", kind="ad_hoc", session="ephemeral"))
    os_.save_team(TeamDef(name="product", lead="product_owner"), "# P\n\n1. x")
    ags = all_agents(project)
    assert ags["product_owner"].source == "role" and ags["product_owner"].team == "product"
    assert members_of(all_teams(project)["product"], ags) == ["product_owner", "writer"]
    store = LocalStore(project)
    v = api.handle(project, store, "get_team", {"name": "product"})["team"]
    assert [a["name"] for a in v["agents"]] == ["product_owner", "writer"]
    assert [a["name"] for a in api.handle(project, store, "list_agents", {})["agents"] if not a["team"] and not a["builtin"]] == []
    # the third case: an agent whose team does not exist yet is still an agent, visibly so
    os_.save_agent(AgentDef(name="retro", roles=["owner"], team="devex", kind="ad_hoc", session="ephemeral", wake_on=["gate_conflict"]))
    listed = {a["name"]: a for a in api.handle(project, store, "list_agents", {})["agents"]}
    assert listed["retro"]["team"] == "devex" and listed["retro"]["team_exists"] is False and listed["writer"]["team_exists"] is True
    shown = {n for t in api.handle(project, store, "list_teams", {})["teams"] for n in [a["name"] for a in t["agents"]]}
    shown |= {n for n, a in listed.items() if not a["team"] or not a["team_exists"]}
    assert {n for n, a in listed.items() if not (a["builtin"] and not a["resident"])} <= shown   # everyone appears somewhere
    from tasking import agents as agents_mod
    assert "`devex`, which has not been created yet" in agents_mod.agent_prompt(project, all_agents(project)["retro"])


def test_effective_tools_and_the_message_tree(project):
    rs, os_ = role_store(project), RepoOrgStore(project)
    rs.save(RoleDef(name="analyst", tools=["events"]), "# You analyse\n\nTranscripts and logs, carefully.")
    rs.save(RoleDef(name="owner", tools=["tickets"]), "# You own\n\nThe backlog, opinionatedly.")
    os_.save_agent(AgentDef(name="po", roles=["owner"], team="product", kind="scheduled", every="6h"))
    os_.save_agent(AgentDef(name="qa_lead", roles=["owner", "analyst"], team="product/qa", kind="scheduled", every="1d"))
    os_.save_agent(AgentDef(name="hunter", roles=["analyst"], team="product/qa", kind="ad_hoc", session="ephemeral"))
    os_.save_team(TeamDef(name="product", lead="po", access=[{"tools": ["escalate"]}]), "# P\n\n1. x")
    os_.save_team(TeamDef(name="product/qa", lead="qa_lead", members=["hunter"], access=[{"tools": ["pipeline"]}]), "# Q\n\n1. y")
    roles, teams, ags = __import__("tasking.roles", fromlist=["all_roles"]).all_roles(project), all_teams(project), all_agents(project)
    assert effective_tools(ags["hunter"], roles, teams) == ("events", "pipeline", "escalate", "team")  # role + team + parent team; `team` so it can reach its lead
    assert effective_tools(ags["qa_lead"], roles, teams) == ("tickets", "events", "pipeline", "escalate", "team")
    assert may_message(teams, ags, "qa_lead", "hunter") is None          # lead to member
    assert may_message(teams, ags, "hunter", "qa_lead") is None          # member to its lead
    assert may_message(teams, ags, "po", "qa_lead") is None              # lead to a sub-team's lead
    assert may_message(teams, ags, "qa_lead", "po") is None              # sub-team lead to its parent's lead
    assert may_message(teams, ags, "supervisor", "hunter") is None
    assert "may not message" in may_message(teams, ags, "po", "hunter")  # grandparent lead skips a level
    assert "may not message" in may_message(teams, ags, "hunter", "po")


def test_agent_prompt_is_roles_then_responsibilities_then_access(project):
    from tasking import agents
    rs, os_ = role_store(project), RepoOrgStore(project)
    rs.save(RoleDef(name="owner", tools=["tickets"]), "# You own the product\n\nBe opinionated about {base_branch}.")
    rs.save(RoleDef(name="analyst", tools=["events"]), "# You analyse\n\nCarefully.")
    os_.save_agent(AgentDef(name="po", roles=["owner"], team="product", kind="scheduled", every="6h"))
    os_.save_agent(AgentDef(name="qa_lead", roles=["owner", "analyst"], team="product/qa", kind="scheduled", every="1d"))
    os_.save_team(TeamDef(name="product", lead="po", access=[{"path": "src", "access": "read"}]), "# Product\n\n1. The backlog is never empty.")
    os_.save_team(TeamDef(name="product/qa", lead="qa_lead"), "# QA\n\n1. Every milestone has an e2e test.")
    p = agents.agent_prompt(project, all_agents(project)["qa_lead"])
    assert p.startswith("# You are the `qa_lead` agent of Orchestra")
    assert "team `product/qa`, which you lead" in p
    assert "You report to `po`, the lead of the team above yours" in p                 # a nested lead knows its own lead
    po = agents.agent_prompt(project, all_agents(project)["po"])
    assert "You have no lead above you except the Supervisor" in po and "message_agent` if you have one" not in po
    assert p.index("## Your role: owner") < p.index("## Your role: analyst") < p.index("## Team `product/qa` (your team)")
    assert "Be opinionated about main." in p                                           # placeholders resolved inside roles
    assert p.index("Every milestone has an e2e test") < p.index("## Team `product` (the team above `product/qa`)")
    assert "The backlog is never empty" in p
    assert "read: `src` (granted to team `product`)" in p and ".orchestra/spaces/product/qa" in p
    assert "## Your access" in p and p.index("## Your access") > p.index("## Team `product`")
    # built-in agents outside a custom team get exactly their role prompt, as before teams existed
    assert agents.agent_prompt(project, all_agents(project)["coordinator"]) == agents.role_prompt(project, "coordinator")


def test_api_builds_a_team_the_way_the_guide_says(project):
    store = LocalStore(project)
    guide = api.handle(project, store, "org_guide", {}, role="supervisor")["guide"]
    assert "One creation per turn" in guide and "ticket_transition" in guide
    with pytest.raises(StoreError):
        api.handle(project, store, "create_role", {"name": "owner", "description": "d", "prompt": "x" * 50}, role="coordinator")
    api.handle(project, store, "create_role", {"name": "owner", "description": "Owns a backlog", "prompt": "# You own\n\n" + "Be opinionated. " * 5,
                                               "tools": ["tickets", "escalate"]}, role="supervisor")
    with pytest.raises(StoreError, match="unknown roles"):
        api.handle(project, store, "create_agent", {"name": "po", "roles": ["nope"], "kind": "scheduled", "every": "6h"}, role="supervisor")
    r = api.handle(project, store, "create_agent", {"name": "po", "roles": ["owner"], "team": "product", "kind": "scheduled", "every": "6h"}, role="supervisor")
    assert r["warning"].startswith("WARN: team 'product' does not exist")      # allowed, and said out loud
    with pytest.raises(StoreError, match="responsibilities are required"):
        api.handle(project, store, "create_team", {"name": "product", "lead": "po"}, role="supervisor")
    r = api.handle(project, store, "create_team", {"name": "product", "lead": "po", "responsibilities": "# Product\n\n1. The backlog is never empty.",
                                                   "access": [{"tools": ["events"]}]}, role="supervisor")
    assert r["storage"].startswith("committed")
    assert "warning" not in api.handle(project, store, "create_agent", {"name": "writer", "roles": ["owner"], "team": "product", "kind": "ad_hoc", "session": "ephemeral"}, role="supervisor")
    api.handle(project, store, "update_team", {"name": "product", "members": ["writer"]}, role="supervisor")
    t = api.handle(project, store, "get_team", {"name": "product"})
    assert t["team"]["lead"] == "po" and [a["name"] for a in t["team"]["agents"]] == ["po", "writer"]
    assert "never empty" in t["responsibilities"] and t["tools_dir"].endswith("spaces/product/tools")
    a = api.handle(project, store, "get_agent", {"name": "po"})
    assert a["tool_groups"] == ["tickets", "escalate", "events", "team"] and a["leads"] == ["product"]
    assert "team" in api.handle(project, store, "get_agent", {"name": "writer"})["tool_groups"]   # members too
    assert "## Team `product` (your team)" in a["prompt"] and a["agent"]["every_seconds"] == 6 * 3600
    # an ephemeral agent's runs are all kept and listed, newest first; its transcript button opens the latest
    store.set_session("writer-20260101-000000", kind="writer", status="idle", started_at=100.0, last_text="first run")
    store.set_session("writer-20260102-000000", kind="writer", status="idle", started_at=200.0, last_text="second run")
    w = api.handle(project, store, "get_agent", {"name": "writer"})
    assert [x["id"] for x in w["runs"]] == ["writer-20260102-000000", "writer-20260101-000000"]
    assert w["agent"]["last_run_id"] == "writer-20260102-000000" and w["agent"]["run_count"] == 2
    # wake-ups: creation queues an introduction for scheduled/continuous agents, never for ad hoc ones
    assert [w["reason"] for w in store.drain_wakeups("po")][0].startswith("you were just created")
    assert store.drain_wakeups("writer") == []
    # the tree: the lead briefs its ad hoc member; the member may not brief the lead; the supervisor may message anyone
    api.handle(project, store, "run_agent", {"name": "writer", "brief": "draft the stories"}, role="po")
    assert store.drain_wakeups("writer")[0]["reason"] == "brief from po: draft the stories"
    with pytest.raises(StoreError, match="may not message"):
        api.handle(project, store, "message_agent", {"name": "po", "note": "hi"}, role="coordinator")
    with pytest.raises(StoreError, match="run_agent is for ad_hoc"):
        api.handle(project, store, "run_agent", {"name": "po", "brief": "x"}, role="supervisor")
    api.handle(project, store, "message_agent", {"name": "po", "note": "check the stories"}, role="supervisor")
    # guards: a role in use and a team with agents cannot be deleted; a lead cannot be deleted
    with pytest.raises(StoreError, match="used by agents"):
        api.handle(project, store, "delete_role", {"name": "owner"}, role="supervisor")
    with pytest.raises(StoreError, match="still has agents"):
        api.handle(project, store, "delete_team", {"name": "product"}, role="supervisor")
    with pytest.raises(StoreError, match="leads"):
        api.handle(project, store, "delete_agent", {"name": "po"}, role="supervisor")
    api.handle(project, store, "pause_agent", {"name": "po"}, role="supervisor")
    assert api.handle(project, store, "list_agents", {})["agents"][-2]["paused"] is True or any(
        x["name"] == "po" and x["paused"] for x in api.handle(project, store, "list_agents", {})["agents"])
    org = api.handle(project, store, "list_roles", {})
    assert next(r for r in org["roles"] if r["name"] == "owner")["agents"] == ["po", "writer"]
    rec = json.loads((project.repo_root / ".orchestra" / "agents" / "po.json").read_text())
    assert rec["team"] == "product" and "builtin" not in rec


def test_custom_agent_with_tickets_tools_may_create_tickets(project):
    store = LocalStore(project)
    sup = {"role": "supervisor"}
    m = store.create_milestone(name="M")
    for name, tools in (("planner", ["tickets"]), ("observer", ["escalate"])):
        api.handle(project, store, "create_role", {"name": name, "description": "d", "prompt": "# You plan\n\n" + "Carefully. " * 6, "tools": tools}, **sup)
        api.handle(project, store, "create_agent", {"name": f"{name}_agent", "roles": [name], "kind": "ad_hoc", "session": "ephemeral"}, **sup)
    r = api.handle(project, store, "create_ticket", {"title": "t", "milestone_id": m.id}, role="planner_agent")
    assert r["ok"]
    with pytest.raises(StoreError, match="may not create tickets"):
        api.handle(project, store, "create_ticket", {"title": "t", "milestone_id": m.id}, role="observer_agent")
