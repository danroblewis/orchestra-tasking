"""The demo, end to end through a real Orchestra: the template copied to a temporary directory,
`orchestra web` on a free port, `tasking run` and the two machines as processes (demo/run.py's
own functions), the tickets created as a planner would (requests over the bus), and the rest
read over HTTP: the task system's API, Orchestra's /api/machines and the Tickets page."""

import socket

import pytest

from demo.run import get, make_project, plan, start, stop, wait_for, wait_until

API = "/api/plugins/tasks"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """The demo running against a temporary home; every process is stopped afterwards, whatever
    the test did."""
    home = tmp_path / "home"
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(home))
    target = make_project(tmp_path / "project")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    procs = start(target, port, env={"ORCHESTRATOR_HOME": str(home)})
    try:
        wait_for(url, procs)
        yield url, target
    finally:
        stop(procs)
        assert all(p.poll() is not None for p in procs.values())


def states(url: str) -> dict[str, str]:
    return {t["id"]: t["state"] for t in get(url + f"{API}/state")["tickets"]}


def settled(url: str) -> bool:
    s = states(url)
    return bool(s) and all(v in ("DONE", "BLOCKED") for v in s.values())


def test_tickets_are_worked_reviewed_and_done_or_blocked_by_programs(demo):
    url, target = demo
    ids = plan(target)
    assert ids == ["DEMO-1", "DEMO-2", "DEMO-3", "DEMO-4"]
    wait_until("the tickets", lambda: settled(url), timeout_s=120)
    assert states(url) == {"DEMO-1": "DONE", "DEMO-2": "DONE", "DEMO-3": "BLOCKED", "DEMO-4": "BLOCKED"}

    # the whole path of a done ticket, applied by the scheduler alone
    evs = get(url + "/api/events")["events"]                       # the last 500: the whole demo is fewer
    trail = [(e["data"]["from"], e["data"]["to"], e["source"]) for e in evs if e["type"] == "ticket_transition" and e.get("subject") == "DEMO-1"]
    assert [(a, b) for a, b, _ in trail] == [("OPEN", "IN_PROGRESS"), ("IN_PROGRESS", "READY_FOR_REVIEW"), ("READY_FOR_REVIEW", "IN_REVIEW"),
                                             ("IN_REVIEW", "APPROVED"), ("APPROVED", "DONE")]
    assert {src for _, _, src in trail} == {"task_scheduler"}
    protocol = [(e["type"], e["source"]) for e in evs if e.get("subject") == "DEMO-1" and e["type"] in ("task.assigned", "task.accepted", "task.reported")]
    assert protocol == [("task.assigned", "task_scheduler"), ("task.accepted", "worker/1"), ("task.reported", "worker/1"),
                        ("task.assigned", "task_scheduler"), ("task.accepted", "reviewer/1"), ("task.reported", "reviewer/1")]
    assert [e["source"] for e in evs if e["type"] == "task.requested"] == ["planner"] * 4

    # what the machines did, as the tickets record it
    one = get(url + f"{API}/tickets/DEMO-1")
    assert one["owner"] is None and [n["author"] for n in one["notes"]] == ["worker", "reviewer"]
    assert one["notes"][1]["text"].startswith("APPROVED")
    assert (target.parent / "home").glob("*/tasks/workspaces/DEMO-1/RESULT.md")
    four = get(url + f"{API}/tickets/DEMO-4")
    assert four["review_rounds"] == 1 and four["worker_turns"] == 2 and "yesterday.txt" in four["blocked_reason"]
    assert any(n["kind"] == "review" and n["text"].startswith("REJECTED") for n in four["notes"])

    # blocked tickets wait for a planner: the attention endpoint every page reads
    attention = get(url + f"{API}/attention")
    assert attention["count"] == 2 and {b["id"] for b in attention["blocked"]} == {"DEMO-3", "DEMO-4"}
    assert "this ticket has none" in next(b["reason"] for b in attention["blocked"] if b["id"] == "DEMO-3")
    board = get(url + f"{API}/board")
    assert board["scheduler"] is True and board["counts"] == {"DONE": 2, "BLOCKED": 2}


def test_orchestra_observes_the_scheduler_and_the_machines(demo):
    url, _target = demo
    seen = {}

    def observed() -> bool:
        d = get(url + "/api/machines")
        if not d.get("ok"):
            return False
        seen.update({m["id"]: m for m in d["machines"]})
        return {"task_scheduler", "worker", "reviewer"} <= set(seen) and not any(seen[m].get("unseen") for m in ("worker", "reviewer"))
    wait_until("the machines", observed, timeout_s=60)
    assert seen["task_scheduler"]["package"] == "tasks" and seen["task_scheduler"]["mode"] == "continuous"
    assert "task.accepted" in seen["task_scheduler"]["subscribes"] and "task.requested" in seen["task_scheduler"]["subscribes"]
    for m in ("worker", "reviewer"):
        assert seen[m]["mode"] == "continuous" and seen[m]["about"]["kind"] == "program" and seen[m]["package"] is None
        assert [i["id"] for i in seen[m]["instances"]] == ["1"]
        assert "task.assigned" in seen[m]["subscribes"]
    assert get(url + "/api/plugins")["pages"] == [{"plugin": "tasks", "path": "/tickets", "title": "Tickets"}]
    html = get_text(url + "/tickets")
    assert "<title>Orchestra · Tickets</title>" in html and "/plugins/tasks/static/tasks.js" in html
    log = get(url + "/api/machines/task_scheduler/log")
    assert log["ok"] and any("project" in line for line in log["lines"])


def get_text(url: str) -> str:
    import urllib.request
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()

