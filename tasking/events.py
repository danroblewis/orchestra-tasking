"""The task system's events, published on Orchestra's bus with the CloudEvents envelope.

The code here calls ``emit(type, src=..., **fields)`` and reads events as flat dicts
(``ev["ticket"]``). ``emit`` publishes a CloudEvent: the fields are its ``data``, the ticket (or
milestone) it is about is its ``subject``, and its ``source`` is the machine it is published as.
``read`` gives the flat shape back: the data's fields beside ``type``, ``ts`` and ``source``.

**The scheduler is no machine** (the owner, 2026-10-27). It is one daemon standing in for each
machine's own scheduler: the worker's queue, the reviewer's, the merge gate, the answers to a
planner's requests. So it never publishes as itself (``task_scheduler``) and never announces
itself: every event it publishes is published as the machine whose responsibility it implements
at that moment (``src``), and only for an act that machine's own process did not already announce
(SPEC.md section 3, "Who publishes what"). Choosing the next ticket to offer (readiness,
priority, capacity) is dispatch, the coordinator's responsibility, so the queue's offers and its
withdrawals of offers nobody took go out as ``coordinator/dispatch``: an instance of the
coordinator machine, beside whatever else fills that role (an AI agent). A publish as
``task_scheduler`` is refused. The web
plugin's operator controls publish as ``task_web``, the command line as ``task_cli``, a planner
asking through ``orchestra tasks api`` as whatever it says it is. The bus itself is opened as Orchestra
opens it (``[events] url`` or ORCHESTRA_EVENTS_URL), so there is one place that decides where the
events go.

**No echoes.** The task system's record is its store: it publishes nothing that restates a step
another machine already announced (a review requested, changes requested, an approval, a claim).
There is no ``ticket.transitioned`` and no ``ticket.note_added``: a ticket's state and notes are
read from the record (the ``get_ticket``, ``list_tickets`` and ``snapshot`` queries, the Tickets
page), and its moves are measured (``tasks_ticket_transitions_total``, tasking/measures.py).

**Causation.** ``emit(..., cause=id)`` names the event this one answers or acts on as its
CloudEvents ``causationid`` (a ticket created on a request names the request).

The names: ``task.*`` is the protocol between the task system and the machines that fill roles
(assignment, requests, attention); ``ticket.created`` and ``milestone.created`` say what a
request made (its id). So a request to create a ticket is ``task.requested`` and what it made is
``ticket.created``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from orchestra.events import Events as OrchestraEvents
from orchestra.events import machine_and_instance

if TYPE_CHECKING:
    from .config import Project

SCHEDULER = "task_scheduler"  # the daemon's name in its log and on the record's notes: never an event's source
WORKER, REVIEWER = "worker", "reviewer"   # the roles a turn is offered to; a held turn taken back is published as its role
OPERATOR = "task_web"         # the operator's console (the web plugin's controls): pause, capacity ceiling, stop
GATE = "merge_gate"           # the merge gate, a machine of the task system's own: it takes approved work
ATTENTION = "task.attention_needed"
COORDINATOR, SUPERVISOR = "coordinator", "supervisor"     # the planner roles attention is addressed to: names, not programs here
DISPATCH = f"{COORDINATOR}/dispatch"   # the queue: the coordinator's dispatch, an instance of that machine (offers, and withdrawing them)


def flat(ev: dict) -> dict:
    """An event as this package reads it: its data's fields, with ``type``, ``ts``, ``source``,
    ``id``, ``subject``, ``causationid`` and ``correlationid`` from the envelope."""
    data = ev.get("data")
    out = dict(data) if isinstance(data, dict) else {"data": data}
    out.update(type=ev.get("type"), ts=ev.get("time"), source=ev.get("source"), id=ev.get("id"),
               subject=ev.get("subject"), causationid=ev.get("causationid"), correlationid=ev.get("correlationid"))
    return out


class Events(OrchestraEvents):
    @classmethod
    def for_project(cls, project: Project, source: str) -> Events:
        return super().for_project(project.orchestra, source=source)

    def publish(self, type: str, data=None, *, source: str | None = None, **ext):
        src = source or self.source
        if machine_and_instance(src)[0] == SCHEDULER:
            raise ValueError(f"{type}: the scheduler publishes as the machine whose responsibility it implements, never as {SCHEDULER}")
        return super().publish(type, data, source=src, **ext)

    def emit(self, type: str, *, src: str | None = None, cause: str | None = None, correlation: str | None = None,
             **data: Any) -> dict:
        """Publish ``type`` with ``data`` as ``src`` (this publisher's own source when None).
        ``cause``: the id of the event this one answers or acts on, named as its causationid;
        ``correlation``: what it belongs to, as its correlationid (a recurring ticket's occurrence)."""
        subject = data.get("ticket") or data.get("milestone")
        ext = {"subject": str(subject)} if subject else {}
        if cause:
            ext["causationid"] = cause
        if correlation:
            ext["correlationid"] = correlation
        return flat(self.publish(type, data or None, source=src, **ext))

    def attention(self, to: str, reason: str, ticket: str | None = None, *, src: str | None = None) -> dict:
        """A planner's decision is needed: a ticket blocked, rejected at every multiple of the
        threshold, a landed orchestra.toml the repository root did not follow, the base branch
        broken, a gate error, the pipeline still paused after a restart, a staging deploy failed or
        could not be restarted. An event for whatever machine fills the planner role ``to``
        (coordinator, supervisor) to listen for, not a write into its queue, published as the
        machine whose work is stuck or whose responsibility raised it (``src``)."""
        return self.emit(ATTENTION, src=src, to=to, reason=reason, **({"ticket": ticket} if ticket else {}))

    def read(self, since: int = 0, limit: int = 500) -> tuple[list[dict], int]:
        evs, nxt = super().read(since, limit)
        return [flat(e) for e in evs], nxt


__all__ = ["ATTENTION", "COORDINATOR", "DISPATCH", "GATE", "OPERATOR", "REVIEWER", "SCHEDULER", "SUPERVISOR", "WORKER", "Events"]
