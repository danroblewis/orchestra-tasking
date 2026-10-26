"""The task system's events, published on Orchestra's bus with the CloudEvents envelope.

The lifted code calls ``emit(type, **fields)`` and reads events as flat dicts (``ev["ticket"]``),
as it did inside Orchestra. Here ``emit`` publishes a CloudEvent: the fields are its ``data``,
the ticket (or milestone) it is about is its ``subject``, and its ``source`` is the machine that
emitted it. Subscribers in this process, and ``read``, get the flat shape the old code expects:
the data's fields beside ``type``, ``ts`` and ``source``.

The source is the machine that emitted the event: the scheduler is ``task_scheduler``, the web
plugin ``tasks-web``, and a ``tasking`` command run by someone else (``ORCHESTRA_SOURCE``) theirs."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestra.bus import EventBus, open_bus
from orchestra.events import Events as OrchestraEvents
from orchestra.events import event_time

if TYPE_CHECKING:
    from .config import Project

LOOP = "task_scheduler"
ATTENTION = "task.attention_needed"


def default_source() -> str:
    return os.environ.get("ORCHESTRA_SOURCE") or LOOP


def flat(ev: dict) -> dict:
    """An event as the lifted code reads it: its data's fields, with ``type``, ``ts``, ``source``."""
    data = ev.get("data")
    if "specversion" not in ev:
        return ev                                                   # already flat (an old event)
    out = dict(data) if isinstance(data, dict) else {"data": data}
    out.update(type=ev.get("type"), ts=ev.get("time"), source=ev.get("source"), id=ev.get("id"))
    return out


class Events(OrchestraEvents):
    def __init__(self, path: Path | None = None, *, bus: EventBus | None = None, source: str | None = None):
        super().__init__(path, bus=bus, source=source or default_source())
        self._flat_subs: list[Callable[[dict], None]] = []

    @classmethod
    def for_project(cls, project: Project, source: str | None = None) -> Events:
        url = os.environ.get("ORCHESTRA_EVENTS_URL") or project.config.events_url
        return cls(project.events_file, bus=open_bus(url, project_key=project.key, file_path=project.events_file),
                   source=source)

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._flat_subs.append(fn)

    def emit(self, type: str, **data: Any) -> dict:
        subject = data.get("ticket") or data.get("milestone")
        ev = self.publish(type, data or None, **({"subject": str(subject)} if subject else {}))
        f = flat(ev)
        for fn in list(self._flat_subs):
            try:
                fn(f)
            except Exception:  # noqa: BLE001, S110 - a subscriber never breaks the publisher
                pass
        return f

    def attention(self, to: str, reason: str, ticket: str | None = None) -> dict:
        """The task system needs a planner's attention (a ticket blocked, rejected again and
        again, done; the pipeline broken): an event the planner's machine listens for, not a
        write into its queue. ``to`` is the role (coordinator, supervisor)."""
        return self.emit(ATTENTION, to=to, reason=reason, **({"ticket": ticket} if ticket else {}))

    def read(self, since: int = 0, limit: int = 500) -> tuple[list[dict], int]:
        evs, nxt = super().read(since, limit)
        return [flat(e) for e in evs], nxt


__all__ = ["LOOP", "Events", "event_time", "flat"]
