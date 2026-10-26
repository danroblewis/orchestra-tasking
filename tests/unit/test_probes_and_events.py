"""Probes turn conditions into events on the transition; agents raise declared events within limits."""

import stat
import subprocess
import time

import pytest

from tasking import api
from tasking.config import init_project
from tasking.events import Events
from tasking.org import (
    AgentDef,
    OrgError,
    RepoOrgStore,
    TeamDef,
    all_agents,
    all_teams,
    event_matches,
)
from tasking.probes import Prober, ProbeSpec, config_probes, team_probes
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


def run_probe(prober, spec):
    prober.tick([spec])
    for th in list(prober.running.values()):
        th.join(30)


def test_probe_emits_only_on_transitions(project, tmp_path):
    store, events = LocalStore(project), Events(project.events_file)
    flag = tmp_path / "broken"
    spec = ProbeSpec(name="readme_ok", command=f"test ! -e {flag}", every=0, timeout=10)
    prober = Prober(project, store, events)
    run_probe(prober, spec)
    assert store.probe_state("readme_ok")["status"] == "ok"
    assert [e["type"] for e in events.read(0)[0] if e["type"].startswith("probe")] == []   # holding from the start: no event
    flag.write_text("1")
    run_probe(prober, spec)
    run_probe(prober, spec)                                                                 # still failing: no second event
    st = store.probe_state("readme_ok")
    assert st["status"] == "failed" and st["since"] <= st["last_run"]
    flag.unlink()
    run_probe(prober, spec)
    kinds = [(e["type"], e.get("probe")) for e in events.read(0)[0] if e["type"].startswith("probe")]
    assert kinds == [("probe_failed", "readme_ok"), ("probe_recovered", "readme_ok")]
    assert "readme_ok" in [p["name"] for p in store.snapshot()["probes"]]
    # not due yet: a probe with an interval is skipped until it elapses
    spec2 = ProbeSpec(name="slow", command="true", every=3600, timeout=10)
    run_probe(prober, spec2)
    first = store.probe_state("slow")["last_run"]
    run_probe(prober, spec2)
    assert store.probe_state("slow")["last_run"] == first
    # a probe no longer declared is marked removed and leaves the snapshot
    prober.tick([])
    assert store.probe_state("slow")["removed"] and "slow" not in [p["name"] for p in store.snapshot()["probes"]]


def test_team_probe_runs_in_the_space_with_tools_on_path(project):
    store, events = LocalStore(project), Events(project.events_file)
    os_ = RepoOrgStore(project)
    role_store(project).save(RoleDef(name="owner", tools=["tickets"]), "# You own\n\nThings, carefully and well.")
    os_.save_agent(AgentDef(name="po", roles=["owner"], team="product", kind="scheduled", every="1d"))
    # The probe finds its tool on PATH and runs it through sh: executing a freshly written
    # binary makes macOS scan it first, which takes long enough to make this test flaky.
    os_.save_team(TeamDef(name="product", lead="po", probes=[{"name": "docs_budget", "command": 'sh "$(command -v doc-budget)"', "every": "30m"}]),
                  "# P\n\n1. The docs are under budget.")
    tool = project.repo_root / ".orchestra" / "spaces" / "product" / "tools" / "doc-budget"
    tool.write_text("#!/bin/sh\ntest \"$(pwd)\" = \"$(cd \"$(dirname \"$0\")/..\" && pwd)\" && test -n \"$STAGING_URL\" && exit 0\necho wrong cwd; exit 1\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    specs = team_probes(project, all_teams(project))
    assert [s.name for s in specs] == ["docs_budget"] and specs[0].every == 1800 and specs[0].team == "product"
    prober = Prober(project, store, events)
    run_probe(prober, specs[0])
    assert store.probe_state("docs_budget")["status"] == "ok", store.probe_state("docs_budget")["output"]
    # declared in orchestra.toml too
    (project.repo_root / "orchestra.toml").write_text((project.repo_root / "orchestra.toml").read_text()
                                                      + '\n[[probes]]\nname = "always"\ncommand = "true"\nevery = "1h"\n')
    project.reload_config()
    assert [s.name for s in config_probes(project)] == ["always"]
    with pytest.raises(OrgError):
        TeamDef(name="tt", lead="a", probes=[{"name": "Bad Name", "command": "x"}]).validate()


def test_agents_subscribe_to_probes_and_declared_events(project):
    assert event_matches(["probe_failed:docs_budget"], {"type": "probe_failed", "probe": "docs_budget"})
    assert not event_matches(["probe_failed:docs_budget"], {"type": "probe_failed", "probe": "other"})
    assert event_matches(["flaky_test_suspected:test_x"], {"type": "flaky_test_suspected", "subject": "test_x"})
    with pytest.raises(OrgError, match="unknown event type"):
        AgentDef(name="a_b", roles=["r"], wake_on=["lesson_proposed"]).validate()
    AgentDef(name="a_b", roles=["r"], wake_on=["lesson_proposed"]).validate({"lesson_proposed"})
    with pytest.raises(OrgError, match="emits entry"):
        TeamDef(name="tt", lead="a", emits=["gate_landed"]).validate()          # Orchestra's own types are not declarable
    # built-in wake lists are documentation: the loop wakes those agents itself
    assert not all_agents(project)["coordinator"].matches_event({"type": "ticket_created"})


def test_emit_event_is_declared_rate_limited_and_depth_limited(project):
    store = LocalStore(project)
    sup = {"role": "supervisor"}
    api.handle(project, store, "create_role", {"name": "analyst", "description": "d", "prompt": "# You analyse\n\n" + "Carefully. " * 6, "tools": ["events"]}, **sup)
    api.handle(project, store, "create_agent", {"name": "keeper", "roles": ["analyst"], "team": "devex", "kind": "scheduled", "every": "1d"}, **sup)
    api.handle(project, store, "create_team", {"name": "devex", "lead": "keeper", "responsibilities": "# DevEx\n\n1. Docs are accurate.",
                                               "emits": ["lesson_proposed"]}, **sup)
    with pytest.raises(StoreError, match="declared by teams"):
        api.handle(project, store, "create_agent", {"name": "retro", "roles": ["analyst"], "team": "devex", "kind": "ad_hoc",
                                                    "session": "ephemeral", "wake_on": ["nope_event"]}, **sup)
    with pytest.raises(StoreError, match="always up"):
        api.handle(project, store, "create_agent", {"name": "retro", "roles": ["analyst"], "team": "devex", "kind": "continuous",
                                                    "wake_on": ["lesson_proposed"], "session": "ephemeral"}, **sup)
    api.handle(project, store, "create_agent", {"name": "retro", "roles": ["analyst"], "team": "devex", "kind": "ad_hoc",
                                                "wake_on": ["lesson_proposed"], "session": "ephemeral"}, **sup)
    with pytest.raises(StoreError, match="may not emit"):
        api.handle(project, store, "emit_event", {"type": "flaky_test_suspected"}, role="keeper")
    with pytest.raises(StoreError, match="cannot be raised"):
        api.handle(project, store, "emit_event", {"type": "gate_landed"}, role="keeper")
    r = api.handle(project, store, "emit_event", {"type": "lesson_proposed", "subject": "CA-30", "note": "plan before editing"}, role="keeper")
    assert r["event"]["depth"] == 1 and r["event"]["by"] == "keeper" and r["wakes"] == ["retro"]
    assert all_agents(project)["retro"].matches_event(r["event"])
    # depth: a turn caused by a three-deep chain may not emit again
    store.set_agent_state("keeper", turn_depth=3)
    with pytest.raises(StoreError, match="deep"):
        api.handle(project, store, "emit_event", {"type": "lesson_proposed"}, role="keeper")
    store.set_agent_state("keeper", turn_depth=0)
    # rate: twenty an hour
    store.set_agent_state("keeper", emits=[time.time()] * 19)
    api.handle(project, store, "emit_event", {"type": "lesson_proposed"}, role="keeper")
    with pytest.raises(StoreError, match="last hour"):
        api.handle(project, store, "emit_event", {"type": "lesson_proposed"}, role="keeper")
    # the supervisor may raise any new type; an old timestamp no longer counts
    store.set_agent_state("keeper", emits=[time.time() - 7200] * 25)
    api.handle(project, store, "emit_event", {"type": "lesson_proposed"}, role="keeper")
    api.handle(project, store, "emit_event", {"type": "direction_changed", "note": "go"}, **sup)
