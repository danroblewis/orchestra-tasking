"""The task system's metrics: levels and the counters that move them, from one record, served as
Prometheus text for the instrument the plugin declares; the scheduler writes none itself."""

import subprocess
import sys

import pytest
from orchestra.metrics import for_project as metrics_for

from tasking import measures
from tasking.events import Events
from tasking.model import State
from tasking.store import LocalStore, StoreError
from tasking.web import collectors, has_test_reports


@pytest.fixture
def store(project):
    return LocalStore(project, Events.for_project(project, source="worker/2"))


def parsed(text: str) -> dict[tuple, float]:
    """Prometheus text lines as {(name, labels): value}."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        series, value = line.rsplit(" ", 1)
        name, _, rest = series.partition("{")
        labels = tuple(sorted(tuple(kv.split("=", 1)) for kv in rest.rstrip("}").replace('"', "").split(",") if kv))
        out[(name, labels)] = float(value)
    return out


def test_ticket_levels_and_flows_balance_and_name_no_writer(store):
    ms = store.create_milestone(name="m")
    a = store.create_ticket(title="a", milestone_id=ms.id)
    store.create_ticket(title="b", milestone_id=ms.id)
    a = store.transition(a.id, a.version, State.IN_PROGRESS)
    store.transition(a.id, a.version, State.READY_FOR_REVIEW)
    store.transition(a.id, a.version + 1, State.IN_REVIEW, by="reviewer")     # the machine whose act it was, named by the caller
    got = parsed(store.metrics_text())
    level = lambda s: got[("tasks_tickets", (("state", s),))]
    assert (level("OPEN"), level("IN_REVIEW"), level("READY_FOR_REVIEW"), level("DONE")) == (1, 1, 0, 0)
    assert got[("tasks_tickets_created_total", (("by", "worker"),))] == 2       # the actor defaults to the store's machine
    moved = lambda f, t, by: got.get(("tasks_ticket_transitions_total", (("by", by), ("from", f), ("to", t))))
    assert moved("OPEN", "IN_PROGRESS", "worker") == 1 and moved("READY_FOR_REVIEW", "IN_REVIEW", "reviewer") == 1
    assert not [k for k in got if "machine" in dict(k[1])]                       # no writer named: a measurement, nobody's
    # the counters live with the tickets, so another process (or a restart) carries on from them
    again = LocalStore(store.project, Events.for_project(store.project, source="worker/1"))
    again.create_ticket(title="c", milestone_id=ms.id)
    assert parsed(again.metrics_text())[("tasks_tickets_created_total", (("by", "worker"),))] == 3


def test_counters_kept_with_a_writer_before_are_summed_without_it():
    counts = {"transition|by=worker|from=OPEN|machine=task_scheduler|to=IN_PROGRESS": 3,
              "transition|by=worker|from=OPEN|to=IN_PROGRESS": 2, "created|by=planner|machine=task_scheduler": 4}
    got = parsed(measures.exposition({}, counts))
    assert got[("tasks_ticket_transitions_total", (("by", "worker"), ("from", "OPEN"), ("to", "IN_PROGRESS")))] == 5
    assert got[("tasks_tickets_created_total", (("by", "planner"),))] == 4


def test_the_scheduler_writes_no_metrics_and_names_whose_move_it_records(project):
    """The scheduler is no machine: it writes no point, and a move it records without naming the
    machine it is for is refused."""
    from tasking.scheduler import Scheduler

    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    planner = LocalStore(project, Events.for_project(project, source="planner"))
    ms = planner.create_milestone(name="m")
    t = planner.create_ticket(title="a", milestone_id=ms.id)
    sched = Scheduler(project, store, events)
    sched.recover()
    store.set_operator_capacity(0, 0)
    sched.tick()
    assert metrics_for(project.orchestra).dump("tasks_", {"project": project.key}) == []
    with pytest.raises(StoreError, match="no machine"):
        store.transition(t.id, t.version, State.CANCELLED)
    with pytest.raises(ValueError, match="never as task_scheduler"):
        events.emit("anything")


def test_the_plugin_declares_the_instrument_and_its_command_prints_the_record(project):
    planner = LocalStore(project, Events.for_project(project, source="planner"))
    planner.create_ticket(title="a", milestone_id=planner.create_milestone(name="m").id)
    [c] = collectors(project)
    assert c.name == "tickets" and c.kind == "collect" and c.every_s == project.orchestra.config.discovery_step
    out = subprocess.run([sys.executable, "-m", "tasking", "--repo", str(project.repo_root), "metrics"], capture_output=True, text=True,
                         check=True).stdout
    assert parsed(out)[("tasks_tickets", (("state", "OPEN"),))] == 1
    assert parsed(out)[("tasks_tickets_created_total", (("by", "planner"),))] == 1


@pytest.mark.skipif(not has_test_reports(),
                    reason="this Orchestra has no test-report instrument (orchestra.instruments.test_reports)")
def test_in_code_mode_the_plugin_also_declares_the_test_report_instrument(project, tmp_path):
    """``tasks.tests``: Orchestra's test-report instrument over the gate's reports, the gate's own
    stages as the kind ``gate``, the project's kind rules after it; its command runs and prints."""
    toml = project.repo_root / "orchestra.toml"
    toml.write_text(toml.read_text().replace('work = "tasks"', 'work = "code"')
                    + '\n[gate.test_reports]\nkinds = ["smoke=tests/smoke/*"]\nconfirm = 2\n')
    project.reload_config()
    tickets, tests = collectors(project)
    assert (tickets.name, tests.name, tests.kind) == ("tickets", "tests", "collect")
    argv = tests.argv()
    assert argv[1:3] == ["-m", "orchestra.instruments.test_reports"] and str(project.reports_dir) in argv[4]
    assert ["--kind", "gate=merge_gate/*", "--kind", "smoke=tests/smoke/*"] == argv[-4:] and "2" == argv[argv.index("--confirm") + 1]
    run = project.reports_dir / "abc"
    run.mkdir(parents=True)
    (run / "tests.xml").write_text('<testsuite><testcase classname="tests.smoke.test_a" name="t"/></testsuite>')
    out = parsed(subprocess.run(argv, capture_output=True, text=True, check=True, cwd=project.repo_root).stdout)
    assert out[("tests", (("kind", "smoke"),))] == 1 and out[("tests_added_total", (("kind", "smoke"),))] == 1
    toml.write_text(toml.read_text().replace("confirm = 2", "enabled = false"))
    project.reload_config()
    assert [c.name for c in collectors(project)] == ["tickets"]
