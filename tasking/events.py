"""The task system's events, published on Orchestra's bus with the CloudEvents envelope.

The code here calls ``emit(type, **fields)`` and reads events as flat dicts (``ev["ticket"]``).
``emit`` publishes a CloudEvent: the fields are its ``data``, the ticket (or milestone) it is
about is its ``subject``, and its ``source`` is the machine that emitted it. ``read`` gives the
flat shape back: the data's fields beside ``type``, ``ts`` and ``source``.

Every publisher names itself as its source: the scheduler is ``task_scheduler``, the web plugin
``task_web``, the command line ``task_cli``, and a planner asking through ``tasking api`` whatever
it says it is. The bus itself is opened as Orchestra opens it (``[events] url`` or
ORCHESTRA_EVENTS_URL), so there is one place that decides where the events go."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestra.bus import EventBus
from orchestra.events import Events as OrchestraEvents

if TYPE_CHECKING:
    from .config import Project

SCHEDULER = "task_scheduler"
ATTENTION = "task.attention_needed"


def flat(ev: dict) -> dict:
    """An event as this package reads it: its data's fields, with ``type``, ``ts``, ``source``."""
    data = ev.get("data")
    out = dict(data) if isinstance(data, dict) else {"data": data}
    out.update(type=ev.get("type"), ts=ev.get("time"), source=ev.get("source"), id=ev.get("id"))
    return out


class Events(OrchestraEvents):
    def __init__(self, path: Path | None = None, *, bus: EventBus | None = None, source: str):
        super().__init__(path, bus=bus, source=source)

    @classmethod
    def for_project(cls, project: Project, source: str) -> Events:
        return cls(bus=OrchestraEvents.for_project(project.orchestra, source=source).bus, source=source)

    def emit(self, type: str, **data: Any) -> dict:
        subject = data.get("ticket") or data.get("milestone")
        return flat(self.publish(type, data or None, **({"subject": str(subject)} if subject else {})))

    def attention(self, to: str, reason: str, ticket: str | None = None) -> dict:
        """The task system needs a planner's attention (a ticket blocked, rejected again and
        again, done; the pipeline broken): an event for whatever machine fills the planner role
        ``to`` (coordinator, supervisor) to listen for, not a write into its queue."""
        return self.emit(ATTENTION, to=to, reason=reason, **({"ticket": ticket} if ticket else {}))

    def read(self, since: int = 0, limit: int = 500) -> tuple[list[dict], int]:
        evs, nxt = super().read(since, limit)
        return [flat(e) for e in evs], nxt


__all__ = ["SCHEDULER", "Events", "flat"]
