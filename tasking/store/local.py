"""The local JSON store: the tickets half (store/tickets.py) and the runtime half
(store/runtime.py) over two files under one lock (store/files.py). SPEC.md section 8."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from orchestra.events import machine_and_instance

from ..config import Project
from ..events import Events
from .files import locked
from .runtime import Runtime
from .tickets import Tickets


class LocalStore(Tickets, Runtime):
    def __init__(self, project: Project, events: Events):
        self.project = project
        self.prefix = project.config.ticket_prefix
        self.events = events                     # what it publishes goes out as this source unless the caller names another
        self.machine = machine_and_instance(events.source)[0] or "unknown"   # the ``by`` of a change no caller names (measures.py)
        project.ensure_dirs()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with locked(self.project.lock_file):
            yield
