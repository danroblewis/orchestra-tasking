"""Goal G14's acceptance (Orchestra's docs/roadmap/goals.md): test health as instruments, end to
end through a real `orchestra run` on a sample-like project in code mode. `orchestra run` starts
the scheduler and the worker (the demo's shell worker, a program that runs a ticket's `sh` blocks
in its worktree and commits), and runs the plugin's instruments every discovery step:
``tasks.tickets`` (the record's counters, the gate's reruns among them) and ``tasks.tests``
(Orchestra's test-report instrument over the gate's reports). A reviewer that approves everything
answers over the bus from this process (tests/fake_machine.py). The gate runs pytest with
``--junitxml={reports}/tests.xml`` and ``[gate] rerun_failures = 1``. Then:

- a ticket that adds three e2e tests lands, and the e2e count in the metrics store rises from 1
  to 4 (a stock of tests written);
- a ticket that breaks one is rejected by the gate, its rerun failing too, and the store shows an
  e2e failure (a regression) while the tests still number 4;
- a ticket that adds a test failing on its first run in each checkout lands on the gate's rerun:
  the record counts one flaky stage (``tasks_gate_flaky_stages_total``) and the test-report
  instrument counts a flaky e2e test and a flaky gate stage;
- a short discovery over the run sees the test counts, as levels (they are too new to be judged
  stocks; Orchestra's tests/unit/test_test_reports_discovery.py replays an hour into a stock).

Skipped when the Orchestra installed has no test-report instrument."""

import shutil
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import free_port, git
from fake_machine import FakeMachine

from demo.run import HERE, start, stop, wait_until
from tasking.web import has_test_reports

pytestmark = pytest.mark.skipif(not has_test_reports(),
                                reason="this Orchestra has no test-report instrument (orchestra.instruments.test_reports)")

TOML = """
[[machines]]
name = "worker"
command = "python machines/shell_worker.py"

[events]
url = "file"

[metrics]
url = "file"

[discovery]
window = "60m"
step = "2s"              # the instruments run every step
every = "60m"

[project]
base_branch = "main"
ticket_prefix = "TH"
work = "code"

[tasks]
workers = 1
reviewers = 1
tick = "1s"

[gate]
timeout = "120s"
rerun_failures = 1

[gate.test_reports]
confirm = 2

[[gate.unit]]
name = "tests"
command = '{python} -m pytest tests -q -p no:cacheprovider --junitxml={{reports}}/tests.xml'
"""

CART = '''
def test_add():
    assert 1 + 1 == 2

def test_remove():
    assert 2 - 1 == 1

def test_total():
    assert sum([1, 2]) == 3
'''

FLAKY = '''
import hashlib, os, pathlib

MARK = pathlib.Path({marks!r}) / hashlib.sha1(os.getcwd().encode()).hexdigest()

def test_eventually():
    if MARK.exists():
        return
    MARK.parent.mkdir(parents=True, exist_ok=True)
    MARK.touch()
    assert False, "fails on its first run in each checkout"
'''


def write_ticket(path: str, text: str) -> str:
    """A ticket description whose one `sh` block writes ``text`` to ``path``."""
    return f"Write `{path}`.\n\n```sh\nmkdir -p {Path(path).parent}\ncat > {path} <<'PY'\n{textwrap.dedent(text).strip()}\nPY\n```"


@pytest.fixture
def running(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(home))
    target = tmp_path / "project"
    (target / "machines").mkdir(parents=True)
    shutil.copy(HERE / "machines" / "shell_worker.py", target / "machines")
    (target / "orchestra.toml").write_text(TOML.format(python=sys.executable))
    (target / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (target / "README.md").write_text("# a sample with tests\n")
    for path, body in {"tests/unit/test_core.py": "def test_one():\n    assert True\n\ndef test_two():\n    assert True\n",
                       "tests/e2e/test_flow.py": "def test_signup():\n    assert True\n"}.items():
        (target / path).parent.mkdir(parents=True, exist_ok=True)
        (target / path).write_text(body)
    git("init", "-q", "-b", "main", cwd=target)
    git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=target)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a sample with tests", cwd=target)
    monkeypatch.setenv("ORCHESTRA_REPO", str(target))
    procs = start(target, free_port(), env={"ORCHESTRATOR_HOME": str(home)})
    try:
        from tasking.config import load_project
        from tasking.events import Events
        project = load_project(target)
        events = Events.for_project(project, source="planner")
        wait_until("the scheduler", lambda: all(p.poll() is None for p in procs.values())
                   and any(e["type"] == "orchestra.machine.started" and e["source"].startswith("worker") for e in events.read(0, limit=1000)[0]),
                   timeout_s=60)
        with FakeMachine(events, roles=("reviewer",)):
            yield project, events, tmp_path
    finally:
        stop(procs)


def series(project, metric: str, /, **labels) -> list[float]:
    """The values written of ``metric{labels}``, in time order (empty when never written)."""
    from orchestra.metrics import for_project as metrics_for
    out = []
    for n, _, points in metrics_for(project.orchestra).dump(metric, {"project": project.key, **labels}):
        if n == metric:
            out += points
    return [v for _, v in sorted(out)]


def test_tests_written_broken_and_flaky_are_measured(running):
    from tasking.requests import ask
    from tasking.store import LocalStore
    project, events, tmp = running
    store = LocalStore(project, events)
    milestone = ask(events, "create_milestone", {"name": "Tests", "description": "More tests."}, "planner")["milestone"]["id"]

    def ticket(title: str, path: str, text: str) -> str:
        out = ask(events, "create_ticket", {"title": title, "milestone_id": milestone, "description": write_ticket(path, text),
                                            "acceptance": "- the gate passes"}, "planner")
        assert out.get("ok"), out
        return out["ticket"]["id"]

    def settles(tid: str, state: str, timeout_s: float = 120) -> None:
        """The ticket in ``state``, and its last gate run finished (the ticket moves before the run's
        record is saved for the last time)."""
        wait_until(f"{tid} {state}", lambda: store.get_ticket(tid).state.value == state
                   and all(r["status"] != "running" for r in store.gate_runs()), timeout_s=timeout_s)

    def measured(metric: str, check, /, timeout_s: float = 30, **labels) -> list[float]:
        wait_until(f"{metric}{labels}", lambda: check(series(project, metric, **labels)), timeout_s=timeout_s)
        return series(project, metric, **labels)

    # a first landing: the gate's first report, with the one e2e test the sample has
    first = ticket("Add a unit test", "tests/unit/test_more.py", "def test_more():\n    assert True\n")
    settles(first, "DONE")
    assert measured("tests", lambda v: v and v[-1] == 1, kind="e2e")[-1] == 1

    # three e2e tests written: the count rises from 1 to 4
    a = ticket("Add cart e2e tests", "tests/e2e/test_cart.py", CART)
    settles(a, "DONE")
    assert series(project, "tests_added_total", kind="e2e")[-1] in (1, 4)          # the first run's one, then these three
    e2e = measured("tests", lambda v: v and v[-1] == 4, kind="e2e")
    assert 1 in e2e and e2e.index(1) < e2e.index(4) and set(e2e) <= {1, 4}
    assert measured("tests", lambda v: v and v[-1] == 3, kind="unit")[-1] == 3

    # one broken: the gate rejects it after its rerun fails too; the store shows the failure
    b = ticket("Change the cart total", "tests/e2e/test_cart.py", CART.replace("== 3", "== 4"))
    settles(b, "BLOCKED")                                  # rejected by the gate; the shell worker cannot act on it
    assert [r["status"] for r in store.gate_runs()] == ["landed", "landed", "rejected"]
    assert store.gate_runs()[-1]["stages"][0]["attempts"] == 2
    assert measured("test_outcomes", lambda v: v and v[-1] == 1, kind="e2e", outcome="failed")[-1] == 1
    assert series(project, "tests", kind="e2e")[-1] == 4

    # a flaky test: it fails the gate's first attempt, passes the rerun, and lands
    c = ticket("Add a flaky e2e test", "tests/e2e/test_flaky.py", FLAKY.format(marks=str(tmp / "marks")))
    settles(c, "DONE")
    landed = store.gate_runs()[-1]
    assert landed["status"] == "landed" and landed["flaky"] == ["unit:tests"]
    assert measured("tasks_gate_flaky_stages_total", lambda v: v and v[-1] == 1, stage="unit", name="tests")[-1] == 1
    assert measured("tasks_gate_reruns_total", lambda v: v and v[-1] == 2, stage="unit", name="tests")[-1] == 2
    assert measured("tests_flaky", lambda v: v and v[-1] >= 1, kind="e2e")[-1] >= 1        # failed, then passed on the rerun
    assert measured("tests_flaky", lambda v: v and v[-1] == 1, kind="gate")[-1] == 1         # the gate's stage, from gate.xml
    assert measured("tests", lambda v: v and v[-1] == 5, kind="e2e")[-1] == 5
    assert series(project, "test_outcomes", kind="e2e", outcome="failed")[-1] == 0              # the regression is gone
    assert series(project, "tests_added_total", kind="e2e")[-1] == 5                             # 1, then 3, then 1
    assert series(project, "tests_removed_total", kind="e2e")[-1] == 0
    assert series(project, "tests_confirmed", kind="e2e")[-1] >= 1                              # test_add, test_remove: passed twice since

    # a short discovery reads the test counts: here as levels, too new to be judged stocks
    from orchestra.metrics import for_project as metrics_for
    from orchestra.sd.rediscover import rediscover
    m = rediscover(project.orchestra, metrics_for(project.orchestra), window_s=600, step_s=2).model
    seen = {k.split("{")[0] for k in m.discovery["left_out"]} | {s.series.name for s in m.stocks if s.series}
    assert {"tests", "test_outcomes", "tests_flaky", "tests_confirmed"} <= seen, seen
