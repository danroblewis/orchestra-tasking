"""The task system's events, published on Orchestra's bus with the CloudEvents envelope.

The code here calls ``emit(type, **fields)`` and reads events as flat dicts (``ev["ticket"]``).
``emit`` publishes a CloudEvent: the fields are its ``data``, the ticket (or milestone) it is
about is its ``subject``, and its ``source`` is the machine that emitted it. ``read`` gives the
flat shape back: the data's fields beside ``type``, ``ts`` and ``source``.

Every publisher names itself as its source: the scheduler is ``task_scheduler``, the web plugin
``task_web``, the command line ``task_cli``, and a planner asking through ``tasking api`` whatever
it says it is. The bus itself is opened as Orchestra opens it (``[events] url`` or
ORCHESTRA_EVENTS_URL), so there is one place that decides where the events go.

The names: ``task.*`` is the protocol between the task system and the machines that fill roles
(assignment, requests, attention); ``ticket.*`` and ``milestone.*`` are the facts the task
system records about its own objects. So a request to create a ticket is ``task.requested`` and
the fact is ``ticket.created``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from orchestra.events import Events as OrchestraEvents

if TYPE_CHECKING:
    from .config import Project

SCHEDULER = "task_scheduler"
ATTENTION = "task.attention_needed"
COORDINATOR, SUPERVISOR = "coordinator", "supervisor"     # the planner roles attention is addressed to: names, not programs here


def flat(ev: dict) -> dict:
    """An event as this package reads it: its data's fields, with ``type``, ``ts``, ``source``."""
    data = ev.get("data")
    out = dict(data) if isinstance(data, dict) else {"data": data}
    out.update(type=ev.get("type"), ts=ev.get("time"), source=ev.get("source"))
    return out


class Events(OrchestraEvents):
    @classmethod
    def for_project(cls, project: Project, source: str) -> Events:
        return super().for_project(project.orchestra, source=source)

    def emit(self, type: str, **data: Any) -> dict:
        subject = data.get("ticket") or data.get("milestone")
        return flat(self.publish(type, data or None, **({"subject": str(subject)} if subject else {})))

    def attention(self, to: str, reason: str, ticket: str | None = None) -> dict:
        """The task system needs a planner's decision: a ticket blocked, rejected at every
        multiple of the threshold, the base branch broken, a gate error, the pipeline still
        paused after a restart, a staging deploy failed. An event for whatever
        machine fills the planner role ``to`` (coordinator, supervisor) to listen for, not a write
        into its queue; routine creations and completions are in ``ticket.*`` and ``milestone.*``."""
        return self.emit(ATTENTION, to=to, reason=reason, **({"ticket": ticket} if ticket else {}))

    def read(self, since: int = 0, limit: int = 500) -> tuple[list[dict], int]:
        evs, nxt = super().read(since, limit)
        return [flat(e) for e in evs], nxt


__all__ = ["ATTENTION", "COORDINATOR", "SCHEDULER", "SUPERVISOR", "Events"]
