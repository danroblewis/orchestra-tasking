"""Probes turn conditions into events on the transition."""

from tasking.events import Events
from tasking.probes import Prober, ProbeSpec, config_probes
from tasking.store import LocalStore


def run_probe(prober, spec):
    prober.tick([spec])
    for th in list(prober.running.values()):
        th.join(30)


def test_probe_emits_only_on_transitions(project, tmp_path):
    events = Events.for_project(project, source="probes")      # as the scheduler publishes them: the condition checks
    store = LocalStore(project, events)
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
    assert kinds == [("probe.failed", "readme_ok"), ("probe.recovered", "readme_ok")]
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
    toml = project.repo_root / "orchestra.toml"
    toml.write_text(toml.read_text() + '\n[[probes]]\nname = "always"\ncommand = "true"\nevery = "1h"\ntimeout = "30s"\n'
                    '\n[[probes]]\nname = "bad"\ncommand = "true"\nevery = "1d"\n')      # Orchestra's durations: ms, s, m, h; no d
    project.reload_config()
    assert [(s.name, s.every, s.timeout) for s in config_probes(project)] == [("always", 3600, 30)]
