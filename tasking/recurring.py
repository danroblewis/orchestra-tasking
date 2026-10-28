"""Recurring tickets (the owner's decision D7: datetime scheduling is the task system's): a ticket
made from a template at each due time of a schedule, declared in ``[[tasks.recurring]]``.

Making a ticket is deciding what the work is, which is a planner's, so an occurrence is published
as ``planner/recurring``, an instance of the planner machine standing in for its scheduled
engagement (D17, D19: the scheduler is no machine and publishes only as the machine whose job it
does). Its ``ticket.created`` carries the occurrence's id as its CloudEvents ``correlationid``
(``<name>@<local due time>``); from there dispatch offers it like any other ticket.

The rules (SPEC.md section 7, "Recurring tickets"):

- **Once.** The record keeps, per definition, the due time of the last occurrence it handled, and
  the store checks it in the same locked write that creates the ticket, so a restart, a second
  tick or a second scheduler never makes an occurrence twice.
- **Catch-up.** A scheduler that was not running (the laptop slept) makes at most one occurrence
  for the time it missed: the latest due one, marked late when it is made more than
  ``late_after`` after its due time. The ones before it are counted as missed, never made.
- **If the last one is still open.** ``if_open = "skip"`` (the default) makes nothing while the
  previous occurrence's ticket is not DONE or CANCELLED, and says so on that ticket;
  ``"create"`` makes one anyway.
- **New, changed, back.** A definition seen for the first time, or back after it was removed,
  disabled or invalid, counts from then: nothing is due before it existed. A changed schedule
  counts from the last occurrence handled.

``check`` reads the definitions and says what is wrong with each, in words a planner (the
Supervisor) can act on; ``tasking recurring check`` prints it."""

from __future__ import annotations

import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from orchestra.config import ConfigError, parse_duration

from .events import Events
from .model import TERMINAL, MilestoneStatus, Note
from .schedule import Schedule, ScheduleError, cron, every, zone
from .store import LocalStore, StoreError

PLANNER = "planner"
SOURCE = f"{PLANNER}/recurring"     # the planner's scheduled engagement: what an occurrence is published as
MACHINE_STARTED = "orchestra.machine.started"
LATE_AFTER_S = 600.0                # an occurrence made more than this after its due time is late
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
TEXTS = ("title", "description", "acceptance", "objective", "verification")
KEYS = {"name", "cron", "every", "at", "on", "timezone", "milestone", "priority", "if_open", "catch_up", "late_after",
        "enabled", *TEXTS}
REFUSED_KEYS = {
    "role": "every ticket is offered to the worker role; a recurring ticket cannot name another",
    "worker_kind": "the task system has no worker kinds yet (goal G3); every ticket is offered to the worker role",
    "kind": "the task system has no worker kinds yet (goal G3); every ticket is offered to the worker role",
    "depends_on": "an occurrence depends on nothing; use if_open = \"skip\" to wait for the previous one",
}
PLACEHOLDERS = {"date", "time", "weekday", "day", "month", "month_name", "year", "week", "quarter", "due"}
OUTCOMES = ("created", "late", "skipped", "missed", "failed")


@dataclass(frozen=True)
class Definition:
    name: str
    schedule: Schedule
    template: dict[str, str]          # title, description, acceptance, objective, verification
    milestone: str
    priority: int = 5
    if_open: str = "skip"
    catch_up: bool = True
    late_after: float = LATE_AFTER_S
    enabled: bool = True


@dataclass
class Checked:
    """The definitions read from the configuration: the valid ones, and what is wrong with the rest
    (``problems``: name or position -> reasons)."""
    definitions: list[Definition] = field(default_factory=list)
    problems: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


def fill(text: str, local: datetime) -> str:
    """A template's text for the occurrence due at ``local`` (its zone's wall time)."""
    iso = local.isocalendar()
    return text.format_map({"date": f"{local:%Y-%m-%d}", "time": f"{local:%H:%M}", "weekday": f"{local:%A}",
                            "day": str(local.day), "month": f"{local:%m}", "month_name": f"{local:%B}",
                            "year": str(local.year), "week": f"{iso.year}-W{iso.week:02d}",
                            "quarter": f"Q{(local.month - 1) // 3 + 1}", "due": local.isoformat(timespec="minutes")})


def _placeholders(text: str) -> list[str]:
    """What is wrong with a template's placeholders: unknown names, indexing, conversions, specs."""
    out = []
    try:
        for _, name, spec, conv in string.Formatter().parse(text):
            if name is None:
                continue
            if name not in PLACEHOLDERS or spec or conv:
                out.append(f"{{{name}{'!' + conv if conv else ''}{':' + spec if spec else ''}}} is not a placeholder; "
                           f"use {', '.join('{' + p + '}' for p in sorted(PLACEHOLDERS))}, or {{{{ and }}}} for braces")
    except ValueError as e:
        out.append(f"{e}: write {{{{ and }}}} for a literal brace")
    return out


def _schedule(d: dict, why: list[str]) -> Schedule | None:
    try:
        tz = zone(d.get("timezone"))
    except ScheduleError as e:
        why.append(str(e))
        return None
    try:
        if "cron" in d and "every" in d:
            raise ScheduleError("give cron or every, not both")
        if "cron" in d:
            if "at" in d or "on" in d:
                raise ScheduleError("cron says the times itself: at and on go with every, not with cron")
            return cron(d["cron"], tz)
        if "every" in d:
            return every(d["every"], d.get("at"), d.get("on"), tz)
        raise ScheduleError("a schedule is required: every = \"day\" | \"week\" | \"month\" with at (and on), or cron = \"m h dom mon dow\"")
    except ScheduleError as e:
        why.append(str(e))
        return None


def _definition(d: Any, why: list[str]) -> Definition | None:
    if not isinstance(d, dict):
        why.append("a recurring definition is a table ([[tasks.recurring]] with name, a schedule, timezone, title, milestone)")
        return None
    for k in sorted(set(d) - KEYS):
        why.append(f"{k}: {REFUSED_KEYS[k]}" if k in REFUSED_KEYS else f"unknown key {k!r}; the keys are {', '.join(sorted(KEYS))}")
    name = d.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        why.append("name is required: lower-case letters, digits, '-', '_' or '.', starting with a letter or digit (\"morning-brief\")")
    sched = _schedule(d, why)
    template = {}
    for k in TEXTS:
        v = d.get(k, "")
        if not isinstance(v, str):
            why.append(f"{k} must be text")
            continue
        why.extend(f"{k}: {p}" for p in _placeholders(v))
        template[k] = v
    if not template.get("title", "").strip():
        why.append("title is required (placeholders such as {date} make each occurrence's own)")
    milestone = d.get("milestone")
    if not isinstance(milestone, str) or not milestone.strip():
        why.append("milestone is required: an open milestone's id (\"M1\") or name; every ticket has one")
    priority = d.get("priority", 5)
    if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 9:
        why.append(f"priority = {priority!r}: a whole number from 1 (soonest) to 9")
    if_open = d.get("if_open", "skip")
    if if_open not in ("skip", "create"):
        why.append(f"if_open = {if_open!r}: \"skip\" (make nothing while the previous one is open) or \"create\"")
    for k in ("catch_up", "enabled"):
        if not isinstance(d.get(k, True), bool):
            why.append(f"{k} must be true or false")
    try:
        late_after = parse_duration(d.get("late_after"), LATE_AFTER_S)
    except ConfigError as e:
        why.append(f"late_after: {e}")
        late_after = LATE_AFTER_S
    if why:
        return None
    return Definition(name=name, schedule=sched, template=template, milestone=milestone.strip(), priority=priority,
                      if_open=if_open, catch_up=d.get("catch_up", True), late_after=late_after, enabled=d.get("enabled", True))


def check(raw: dict, store: LocalStore | None = None) -> Checked:
    """The definitions in a configuration (Orchestra's ``project.config.raw``) and what is wrong
    with each. With a ``store``, a milestone that does not exist or is closed is wrong too."""
    out = Checked()
    items = (raw.get("tasks") or {}).get("recurring", [])
    if not isinstance(items, list):
        out.problems["tasks.recurring"] = ["write each definition as [[tasks.recurring]] (an array of tables)"]
        return out
    seen: set[str] = set()
    for i, d in enumerate(items):
        why: list[str] = []
        label = d.get("name") if isinstance(d, dict) and isinstance(d.get("name"), str) and d.get("name") else f"#{i + 1}"
        defn = _definition(d, why)
        if defn and defn.name in seen:
            why.append(f"another definition is already named {defn.name}")
        if defn and store is not None and not why:
            try:
                milestone_of(store, defn.milestone)
            except StoreError as e:
                why.append(str(e))
        if why:
            out.problems[label if label not in out.problems and label not in seen else f"{label} (#{i + 1})"] = why
        else:
            out.definitions.append(defn)
            seen.add(defn.name)
    return out


def milestone_of(store: LocalStore, ref: str) -> str:
    """The id of the open milestone ``ref`` names (its id, else its name)."""
    ms = store.milestones_by_id()
    m = ms.get(ref) or next(iter([x for x in ms.values() if x.name == ref]), None)
    if m is None:
        raise StoreError(f"milestone {ref!r}: no milestone has that id or name")
    if m.status != MilestoneStatus.OPEN:
        raise StoreError(f"milestone {ref!r} ({m.id}) is closed: a ticket needs an open one")
    if m.id != ref and sum(1 for x in ms.values() if x.name == ref) > 1:
        raise StoreError(f"milestone {ref!r}: several milestones have that name; give the id")
    return m.id


def occurrence_id(name: str, local: datetime) -> str:
    return f"{name}@{local:%Y-%m-%dT%H:%M}"


class Recurring:
    """The planner's scheduled engagement, run by the scheduler every tick (``tick``)."""

    def __init__(self, project, store: LocalStore, events: Events, log=print):
        self.project, self.store, self.events, self.log = project, store, events, log
        self.said: set[str] = set()          # problems already logged, so a broken definition is said once
        self.announced = False
        self.clock = time.time               # what "now" is (a test may give its own)

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        checked = check(self.project.config.raw)
        for label, why in checked.problems.items():
            said = f"{label}: {'; '.join(why)}"
            if said not in self.said:
                self.said.add(said)
                self.log(f"recurring {label} is not used: {'; '.join(why)} (tasking recurring check)")
        live = [d for d in checked.definitions if d.enabled]
        if live and not self.announced:
            self.events.publish(MACHINE_STARTED, {"mode": "scheduled", "kind": "program", "listens_for": [],
                                                  "recurring": [d.name for d in live]}, source=SOURCE)
            self.announced = True
        self.store.recurring_seen([d.name for d in live], now)
        state = self.store.recurring()
        for d in live:
            try:
                self._due(d, state.get(d.name) or {}, now)
            except Exception as e:  # noqa: BLE001 - one broken definition never stops the others
                self.log(f"recurring {d.name}: {e!r}")

    def _due(self, d: Definition, st: dict, now: float) -> None:
        """The latest due time since the last one handled, if any, handled: made (on time or
        late), skipped, or missed; the ones before it missed."""
        last = st.get("due")
        if last is None:
            return
        due = list(d.schedule.between(float(last), now))
        if not due:
            return
        ts, local = due[-1]
        occ = occurrence_id(d.name, local)
        missed = len(due) - 1
        late = now - ts > d.late_after
        if late and not d.catch_up:
            self.store.settle_occurrence(d.name, due=ts, occurrence=occ, outcome="missed", missed=missed)
            self.log(f"recurring {d.name}: {occ} missed ({int(now - ts)} s late; catch_up = false)")
            return
        prev = st.get("ticket")
        if d.if_open == "skip" and prev:
            try:
                t = self.store.get_ticket(prev)
            except StoreError:
                t = None
            if t is not None and t.state not in TERMINAL:
                if self.store.settle_occurrence(d.name, due=ts, occurrence=occ, outcome="skipped", missed=missed):
                    self.store.add_note(prev, Note(author=PLANNER, text=f"recurring {d.name}: the occurrence {occ} was not made, "
                                                                        f"since this one is still {t.state} (if_open = \"skip\")"))
                    self.log(f"recurring {d.name}: {occ} skipped: {prev} is still {t.state}")
                return
        fields = {k: fill(v, local) for k, v in d.template.items()}
        try:
            ticket = {**fields, "milestone_id": milestone_of(self.store, d.milestone), "priority": d.priority, "created_by": PLANNER}
            t = self.store.settle_occurrence(d.name, due=ts, occurrence=occ, outcome="late" if late else "created", missed=missed,
                                             ticket=ticket, src=SOURCE, late=late)
        except StoreError as e:            # no open milestone: handled as failed, said once, never tried again every tick
            if self.store.settle_occurrence(d.name, due=ts, occurrence=occ, outcome="failed", missed=missed):
                self.events.attention("supervisor", f"recurring ticket {d.name}: the occurrence {occ} could not be made: {e}; "
                                                    "fix [[tasks.recurring]] (tasking recurring check)", src=SOURCE)
                self.log(f"recurring {d.name}: {occ} not made: {e}")
            return
        if t:
            self.log(f"recurring {d.name}: {t.id} made for {occ}" + (f", late by {int(now - ts)} s" if late else "")
                     + (f"; {missed} earlier one(s) missed" if missed else ""))


def view(project, store: LocalStore, now: float | None = None, raw: dict | None = None) -> dict:
    """What the pages and ``tasking recurring list`` show: each definition with its schedule in
    words, its next due time, and the last occurrence handled; and what is wrong with the rest.
    ``raw``: a configuration to check instead of the project's (a planner's draft)."""
    now = time.time() if now is None else now
    checked = check(project.config.raw if raw is None else raw, store)
    state = store.recurring()
    out = []
    for d in checked.definitions:
        nxt = d.schedule.next_after(max(now, float((state.get(d.name) or {}).get("due") or 0)))
        st = state.get(d.name) or {}
        out.append({"name": d.name, "schedule": d.schedule.text, "timezone": d.schedule.tz.key, "enabled": d.enabled, "if_open": d.if_open,
                    "next_due": nxt[0] if nxt and d.enabled else None,
                    "next_local": nxt[1].isoformat(timespec="minutes") if nxt and d.enabled else None,
                    "last": {k: st.get(k) for k in ("occurrence", "outcome", "ticket", "at")} if st.get("occurrence") else None})
    return {"ok": checked.ok, "now": now, "recurring": out, "problems": checked.problems}
