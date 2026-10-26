"""Probes turn conditions into events on the transition."""

import subprocess

import pytest

from tasking.config import init_project
from tasking.events import Events
from tasking.probes import Prober, ProbeSpec, config_probes
from tasking.store import LocalStore


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


def test_probes_are_declared_in_orchestra_toml(project):
    (project.repo_root / "orchestra.toml").write_text((project.repo_root / "orchestra.toml").read_text()
                                                      + '\n[[probes]]\nname = "always"\ncommand = "true"\nevery = "1h"\n')
    project.reload_config()
    assert [(s.name, s.every) for s in config_probes(project)] == [("always", 3600)]
