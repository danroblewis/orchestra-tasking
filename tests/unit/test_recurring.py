"""Recurring tickets (tasking/recurring.py; D7): made once per due time, as the planner's
scheduled engagement, with at most one late occurrence after a sleep, skipped while the last one
is open, and every definition checked with reasons. The clock is the test's (``tick(now=...)``)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import on_bus

from tasking.events import Events
from tasking.measures import OCCURRENCES, counters
from tasking.model import State
from tasking.recurring import SOURCE, Recurring, check, view
from tasking.store import LocalStore

LONDON = ZoneInfo("Europe/London")
BRIEF = """
[[tasks.recurring]]
name = "morning-brief"
every = "day"
at = "07:30"
timezone = "Europe/London"
title = "Morning brief for {weekday} {date}"
description = "Write the brief for {date}."
acceptance = "- the brief is in briefs/{date}.md"
milestone = "M1"
priority = 2
"""


def at(*args) -> float:
    return datetime(*args, tzinfo=LONDON).timestamp()


def configure(project, text: str):
    toml = project.repo_root / "orchestra.toml"
    toml.write_text(toml.read_text() + text)
    project.reload_config()


@pytest.fixture
def rec(project):
    """A store with milestone M1 and the morning brief configured; the recurring engagement and
    its log."""
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    store.create_milestone(src="planner", name="Operations")
    configure(project, BRIEF)
    said: list[str] = []
    r = Recurring(project, store, events, log=said.append)
    r.said_lines = said
    return r


def made(r) -> list:
    return [t for t in r.store.list_tickets() if t.occurrence]


def outcomes(r) -> dict[str, int]:
    return {dict(labels)["outcome"]: n for (name, labels), n in counters(r.store._t()["metric_counts"]).items() if name == OCCURRENCES}


def test_an_occurrence_is_made_at_its_due_time_as_the_planner_with_its_correlation_id(rec):
    rec.tick(now=at(2026, 10, 26, 12, 0))              # first seen: counts from now; this morning's is not due
    assert made(rec) == []
    rec.tick(now=at(2026, 10, 27, 7, 29))
    assert made(rec) == []
    rec.tick(now=at(2026, 10, 27, 7, 30, 2))
    [t] = made(rec)
    assert t.title == "Morning brief for Tuesday 2026-10-27" and t.description == "Write the brief for 2026-10-27."
    assert t.acceptance == "- the brief is in briefs/2026-10-27.md" and t.priority == 2 and t.milestone_id == "M1"
    assert t.occurrence == "morning-brief@2026-10-27T07:30" and t.created_by == "planner" and t.state == State.OPEN
    [ev] = on_bus(rec.events, "ticket.created", ticket=t.id)
    assert ev["source"] == SOURCE == "planner/recurring"                  # the planner's act, never the scheduler's
    assert ev["correlationid"] == t.occurrence and "causationid" not in ev
    assert ev["data"]["recurring"] == "morning-brief" and ev["data"]["late"] is False
    [hello] = on_bus(rec.events, "orchestra.machine.started")
    assert hello["source"] == SOURCE and hello["data"]["mode"] == "scheduled" and hello["data"]["kind"] == "program"
    assert outcomes(rec) == {"created": 1}
    assert 'tasks_tickets_created_total{by="planner"} 1' in rec.store.metrics_text()
    assert 'tasks_recurring_occurrences_total{outcome="created",recurring="morning-brief"} 1' in rec.store.metrics_text()


def test_a_restart_or_a_second_writer_never_makes_an_occurrence_twice(rec, project):
    rec.tick(now=at(2026, 10, 26, 12, 0))
    rec.tick(now=at(2026, 10, 27, 7, 31))
    rec.tick(now=at(2026, 10, 27, 7, 32))                # the next tick
    again = Recurring(project, LocalStore(project, rec.events), rec.events, log=lambda _: None)
    again.tick(now=at(2026, 10, 27, 7, 33))              # a scheduler started again
    assert len(made(rec)) == 1
    # the store refuses a due time already handled, whoever asks: two writers racing for the same occurrence
    assert rec.store.settle_occurrence("morning-brief", due=at(2026, 10, 27, 7, 30), occurrence="morning-brief@2026-10-27T07:30",
                                       outcome="created", ticket={"title": "x", "milestone_id": "M1"}, src=SOURCE) is False
    assert len(made(rec)) == 1 and len(on_bus(rec.events, "ticket.created")) == 1


def test_after_a_sleep_at_most_one_occurrence_is_made_marked_late_and_the_rest_are_missed(rec):
    rec.tick(now=at(2026, 10, 20, 12, 0))
    rec.tick(now=at(2026, 10, 21, 7, 30, 5))             # Wednesday's, on time; then it is done
    [first] = made(rec)
    rec.store.transition(first.id, None, State.CANCELLED, by="planner")
    rec.tick(now=at(2026, 10, 27, 9, 0))                 # the laptop slept from Wednesday to Tuesday 09:00
    tickets = made(rec)
    assert len(tickets) == 2
    late = tickets[-1]
    assert late.occurrence == "morning-brief@2026-10-27T07:30"            # the latest one only
    assert on_bus(rec.events, "ticket.created", ticket=late.id)[0]["data"]["late"] is True
    assert outcomes(rec) == {"created": 1, "late": 1, "missed": 5}          # Thursday to Monday counted, never made
    assert any("late by 5400 s; 5 earlier one(s) missed" in line for line in rec.said_lines)
    rec.tick(now=at(2026, 10, 27, 9, 1))
    assert len(made(rec)) == 2


def test_without_catch_up_a_late_occurrence_is_missed_not_made(rec, project):
    toml = project.repo_root / "orchestra.toml"
    toml.write_text(toml.read_text().replace('priority = 2\n', 'priority = 2\ncatch_up = false\nlate_after = "30m"\n'))
    project.reload_config()
    rec.tick(now=at(2026, 10, 25, 12, 0))
    rec.tick(now=at(2026, 10, 26, 7, 50))                # 20 minutes late: within late_after, made (late = false)
    assert len(made(rec)) == 1
    rec.store.transition(made(rec)[0].id, None, State.CANCELLED, by="planner")
    rec.tick(now=at(2026, 10, 27, 9, 0))                 # 90 minutes late: missed
    assert len(made(rec)) == 1 and outcomes(rec) == {"created": 1, "missed": 1}


def test_skip_while_the_previous_occurrence_is_open_and_create_anyway(rec, project):
    rec.tick(now=at(2026, 10, 25, 12, 0))
    rec.tick(now=at(2026, 10, 26, 7, 30, 1))
    [monday] = made(rec)
    rec.tick(now=at(2026, 10, 27, 7, 30, 1))             # Monday's still OPEN: Tuesday's is skipped
    assert made(rec) == [monday] and outcomes(rec) == {"created": 1, "skipped": 1}
    assert "morning-brief@2026-10-27T07:30 was not made" in rec.store.notes(monday.id)[-1]["text"]
    rec.store.transition(monday.id, None, State.CANCELLED, by="planner")
    rec.tick(now=at(2026, 10, 28, 7, 30, 1))             # done with: Wednesday's is made
    assert [t.occurrence for t in made(rec)] == ["morning-brief@2026-10-26T07:30", "morning-brief@2026-10-28T07:30"]
    toml = project.repo_root / "orchestra.toml"
    toml.write_text(toml.read_text().replace('priority = 2\n', 'priority = 2\nif_open = "create"\n'))
    project.reload_config()
    rec.tick(now=at(2026, 10, 29, 7, 30, 1))             # Wednesday's still open, made anyway
    assert len(made(rec)) == 3 and outcomes(rec)["created"] == 3


def test_a_new_or_returning_definition_counts_from_when_it_is_seen(rec, project):
    rec.tick(now=at(2026, 10, 27, 9, 0))                 # added at 09:00: this morning's 07:30 is not owed
    assert made(rec) == []
    toml = project.repo_root / "orchestra.toml"
    text = toml.read_text()
    toml.write_text(text.replace('name = "morning-brief"', 'name = "morning-brief"\nenabled = false'))
    project.reload_config()
    rec.tick(now=at(2026, 10, 28, 9, 0))                 # disabled across Wednesday's
    toml.write_text(text)
    project.reload_config()
    rec.tick(now=at(2026, 10, 30, 9, 0))                 # back on Friday: nothing owed for the days it was off
    assert made(rec) == []
    rec.tick(now=at(2026, 10, 31, 7, 30, 1))
    assert len(made(rec)) == 1


def test_a_closed_milestone_fails_the_occurrence_once_and_asks_the_supervisor(rec):
    rec.tick(now=at(2026, 10, 26, 12, 0))
    rec.store.close_milestone("M1")
    rec.tick(now=at(2026, 10, 27, 7, 30, 1))
    rec.tick(now=at(2026, 10, 27, 7, 30, 4))
    assert made(rec) == [] and outcomes(rec) == {"failed": 1}
    [ask] = [e for e in on_bus(rec.events, "task.attention_needed") if e["data"]["to"] == "supervisor"]
    assert ask["source"] == SOURCE and "M1" in ask["data"]["reason"] and "closed" in ask["data"]["reason"]


def test_the_next_due_times_are_shown(rec):
    rec.tick(now=at(2026, 10, 27, 9, 0))
    v = view(rec.project, rec.store, now=at(2026, 10, 27, 9, 0))
    [r] = v["recurring"]
    assert v["ok"] and r["name"] == "morning-brief" and r["schedule"] == "daily at 07:30 Europe/London"
    assert r["next_local"] == "2026-10-28T07:30+00:00" and r["next_due"] == at(2026, 10, 28, 7, 30)
    assert r["last"] is None


@pytest.mark.parametrize(("toml", "reasons"), [
    ('name = "x"\nevery = "day"\nat = "07:00"\ntitle = "t"\nmilestone = "M1"', ["timezone is required"]),
    ('name = "x"\ntimezone = "UTC"\ntitle = "t"\nmilestone = "M1"', ["a schedule is required"]),
    ('name = "x"\ncron = "0 7 * * *"\nat = "07:00"\ntimezone = "UTC"\ntitle = "t"\nmilestone = "M1"', ["at and on go with every"]),
    ('name = "x"\ncron = "0 7 * * *"\nevery = "day"\ntimezone = "UTC"\ntitle = "t"\nmilestone = "M1"', ["cron or every, not both"]),
    ('name = "Bad Name"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\ntitle = "t"\nmilestone = "M1"', ["name is required"]),
    ('name = "x"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\nmilestone = "M1"', ["title is required"]),
    ('name = "x"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\ntitle = "Brief {data}"\nmilestone = "M1"', ["{data} is not a placeholder"]),
    ('name = "x"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\ntitle = "Brief {date:%d}"\nmilestone = "M1"', ["{date:%d} is not a placeholder"]),
    ('name = "x"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\ntitle = "t {"\nmilestone = "M1"', ["literal brace"]),
    ('name = "x"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\ntitle = "t"', ["milestone is required"]),
    ('name = "x"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\ntitle = "t"\nmilestone = "M1"\npriority = 0', ["1 (soonest) to 9"]),
    ('name = "x"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\ntitle = "t"\nmilestone = "M1"\nif_open = "wait"', ['"skip"']),
    ('name = "x"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\ntitle = "t"\nmilestone = "M1"\ncatch_up = "yes"', ["true or false"]),
    ('name = "x"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\ntitle = "t"\nmilestone = "M1"\nlate_after = "soon"', ["late_after"]),
    ('name = "x"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\ntitle = "t"\nmilestone = "M1"\nworker_kind = "writer"', ["no worker kinds yet"]),
    ('name = "x"\nevery = "day"\nat = "07:00"\ntimezone = "UTC"\ntitle = "t"\nmilestone = "M1"\ntitel = "t"', ["unknown key 'titel'"]),
    ('name = "x"\nevery = "week"\nat = "07:00"\ntimezone = "UTC"\ntitle = "t"\nmilestone = "M1"', ["needs on: the weekday"]),
])
def test_an_invalid_definition_is_refused_with_its_reasons(toml, reasons):
    import tomllib
    checked = check(tomllib.loads("[[tasks.recurring]]\n" + toml))
    assert checked.definitions == [] and not checked.ok
    [(_label, why)] = checked.problems.items()
    for reason in reasons:
        assert any(reason in w for w in why), why


def test_check_with_the_store_refuses_a_missing_milestone_and_a_duplicate_name_and_keeps_the_rest(rec, project):
    configure(project, BRIEF.replace('milestone = "M1"', 'milestone = "Nowhere"').replace("morning-brief", "evening"))
    configure(project, BRIEF)
    checked = check(project.config.raw, rec.store)
    assert [d.name for d in checked.definitions] == ["morning-brief"]
    assert checked.problems == {"evening": ["milestone 'Nowhere': no milestone has that id or name"],
                                "morning-brief (#3)": ["another definition is already named morning-brief"]}
    assert [d.milestone for d in check(project.config.raw).definitions] == ["M1", "Nowhere"]   # without the store, by name only
    rec.tick(now=at(2026, 10, 27, 9, 0))
    assert sum("is not used" in line for line in rec.said_lines) == 1       # the duplicate said once in the log...
    rec.tick(now=at(2026, 10, 27, 9, 1))
    assert sum("is not used" in line for line in rec.said_lines) == 1       # ...not every tick


def test_a_milestone_by_name(rec, project):
    configure(project, BRIEF.replace('milestone = "M1"', 'milestone = "Operations"').replace("morning-brief", "by-name"))
    rec.tick(now=at(2026, 10, 26, 12, 0))
    rec.tick(now=at(2026, 10, 27, 7, 30, 1))
    assert {t.milestone_id for t in made(rec)} == {"M1"} and len(made(rec)) == 2


def test_the_check_recurring_query_checks_the_configured_ones_or_a_draft(rec, project):
    """What a planner (the Supervisor) asks over the bus before and after it writes definitions."""
    from tasking.api import handle
    from tasking.requests import QUERIES
    assert "check_recurring" in QUERIES
    now = handle(project, rec.store, rec.events, "check_recurring", {})
    assert now["ok"] and now["valid"] and [r["name"] for r in now["recurring"]] == ["morning-brief"] and now["problems"] == {}
    draft = [{"name": "dr-exercise", "every": "month", "on": 1, "at": "10:00", "timezone": "America/New_York",
              "title": "DR exercise {month_name} {year}", "milestone": "Operations"},
             {"name": "stale-prs", "every": "day", "at": "9:00", "title": "Nudge stale PRs", "milestone": "M9"}]
    out = handle(project, rec.store, rec.events, "check_recurring", {"definitions": draft})
    assert out["ok"] and not out["valid"]
    [dr] = out["recurring"]
    assert dr["name"] == "dr-exercise" and dr["schedule"] == "monthly on 1 at 10:00 America/New_York"
    assert dr["next_local"][8:16] == "01T10:00"                                  # the 1st, 10:00 New York time
    assert out["problems"]["stale-prs"] == ["timezone is required: an IANA name such as \"Europe/London\", \"America/New_York\" or \"UTC\""]
