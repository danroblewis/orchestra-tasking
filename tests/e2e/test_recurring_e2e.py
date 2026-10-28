"""Recurring tickets end to end (D7): made at their due time as the planner's scheduled
engagement (``planner/recurring``), offered by dispatch, worked, reviewed and landed like any
ticket.

In process: the real scheduler, store, git and gate in code mode, with machines that speak the
protocol (tests/fake_machine.py) and the recurring engagement's clock set by the test, so a whole
week passes in seconds, a restart in the middle. As processes: the demo through a real
`orchestra run` and `orchestra web` (demo/run.py's functions), on the wall clock, with a
definition due every minute: its ticket appears at the minute, is done by the demo's programs,
and its next due time is on the Tasks panel's route."""

import time
from datetime import UTC, datetime, timedelta

from conftest import free_port, git, handoffs, on_bus
from fake_machine import FakeMachine
from test_handoffs_e2e import setup

from demo.run import Web, make_project, start, stop, wait_for, wait_until, web_token
from tasking.assignments import APPROVED, ASSIGNED, REVIEW_REQUESTED
from tasking.model import State
from tasking.recurring import SOURCE
from tasking.scheduler import Scheduler
from tasking.store import LocalStore

WEEKLY = """
[[tasks.recurring]]
name = "weekly-update"
every = "week"
on = "mon"
at = "09:00"
timezone = "America/New_York"
title = "Weekly update, week of {date}"
description = "Write the weekly update for {week}."
acceptance = "- WORK.md has the update"
milestone = "M1"
"""


def until(sched, check, timeout_s=30.0):
    end_at = time.time() + timeout_s
    while time.time() < end_at:
        sched.tick()
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("did not happen in time")


def test_a_weekly_ticket_is_made_on_monday_morning_worked_and_landed_and_a_restart_makes_no_second(project):
    store, events, first = setup(project, "code")             # one planner's ticket first, M1 its milestone
    toml = project.repo_root / "orchestra.toml"
    toml.write_text(toml.read_text() + WEEKLY)
    project.reload_config()
    git("commit", "-qam", "weekly update", cwd=project.repo_root)
    clock = [datetime(2026, 10, 30, 12, 0, tzinfo=UTC).timestamp()]      # a Friday
    sched = Scheduler(project, store, events)
    sched.recurring.clock = lambda: clock[0]
    sched.recover()
    sched.announce()
    with FakeMachine(events):
        until(sched, lambda: store.get_ticket(first.id).state == State.DONE)
        clock[0] = datetime(2026, 11, 2, 13, 59, tzinfo=UTC).timestamp()    # Monday 08:59 New York (EST from Sunday)
        sched.tick()
        assert [t.id for t in store.list_tickets() if t.occurrence] == []
        clock[0] += 61                                                       # 09:00:01
        until(sched, lambda: any(t.occurrence for t in store.list_tickets()) and
              next(t for t in store.list_tickets() if t.occurrence).state == State.DONE)
    [t] = [t for t in store.list_tickets() if t.occurrence]
    assert t.title == "Weekly update, week of 2026-11-02" and t.occurrence == "weekly-update@2026-11-02T09:00"
    assert f"work on {t.id}\n" in git("show", "main:WORK.md", cwd=project.repo_root)            # landed on main by the gate
    [made] = on_bus(events, "ticket.created", ticket=t.id)
    assert made["source"] == SOURCE and made["correlationid"] == t.occurrence
    assert handoffs(events, t.id) == [("planner", "ticket.created", "coordinator"), ("coordinator", ASSIGNED, "worker"),
                                      ("worker", REVIEW_REQUESTED, "reviewer"), ("reviewer", APPROVED, "merge_gate")]
    assert not [e for e in events.bus.read(0, limit=100000)[0] if e["source"] == "task_scheduler"]
    # a restart on the same Monday: the occurrence stands, none is made again
    again = Scheduler(project, LocalStore(project, events), events)
    again.recurring.clock = lambda: clock[0] + 3600
    again.recover()
    again.tick()
    assert len([x for x in store.list_tickets() if x.occurrence]) == 1


def test_the_demo_makes_a_recurring_ticket_at_its_minute_and_does_it(tmp_path, monkeypatch):
    """Through a real `orchestra run` on the wall clock: a definition due every minute (UTC), its
    ticket made at the minute and done by the demo's worker and reviewer programs; the route the
    Tasks panel reads shows the next due time."""
    home = tmp_path / "home"
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(home))
    target = make_project(tmp_path / "project")
    (target / "orchestra.toml").write_text((target / "orchestra.toml").read_text() + """
[[tasks.recurring]]
name = "minute-note"
cron = "* * * * *"
timezone = "UTC"
title = "Note for {date} {time}"
description = "```sh\\necho noted > RESULT.md\\n```"
acceptance = "- `RESULT.md` exists"
milestone = "Notes"
""")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "a recurring note", cwd=target)
    port = free_port()
    procs = start(target, port, env={"ORCHESTRATOR_HOME": str(home)})
    try:
        web = Web(f"http://127.0.0.1:{port}", web_token(target))
        wait_for(web, procs)
        from tasking.config import load_project
        from tasking.events import Events
        from tasking.requests import ask
        events = Events.for_project(load_project(target), source="planner")     # the milestone, asked for as a planner does
        assert ask(events, "create_milestone", {"name": "Notes"}, "planner")["ok"]
        rec = web.get("/api/plugins/tasks/recurring")
        assert rec["ok"] and [r["name"] for r in rec["recurring"]] == ["minute-note"]
        board = web.get("/api/plugins/tasks/board")
        [r] = board["recurring"]
        assert r["schedule"] == "cron * * * * *" and r["next_due"] > time.time() - 1
        minute = datetime.fromtimestamp(r["next_due"], UTC)
        wait_until("a recurring ticket", lambda: any(t.get("occurrence") for t in web.get("/api/plugins/tasks/state")["tickets"]), 90)
        made = next(t for t in web.get("/api/plugins/tasks/state")["tickets"] if t.get("occurrence"))
        assert made["occurrence"] == f"minute-note@{minute:%Y-%m-%dT%H:%M}" and made["title"] == f"Note for {minute:%Y-%m-%d %H:%M}"
        created = datetime.fromisoformat(made["created_at"])
        assert minute <= created < minute + timedelta(seconds=15)                     # at its due time, not before
        wait_until("the recurring ticket done", lambda: next(t for t in web.get("/api/plugins/tasks/state")["tickets"]
                                                              if t["id"] == made["id"])["state"] == "DONE", 90)
        later = web.get("/api/plugins/tasks/recurring")["recurring"][0]
        assert later["last"]["ticket"] in {t["id"] for t in web.get("/api/plugins/tasks/state")["tickets"] if t.get("occurrence")}
        assert 'tasks_recurring_occurrences_total{outcome="created",recurring="minute-note"}' in web.text("/api/plugins/tasks/metrics")
    finally:
        stop(procs)
