"""The local JSON store: the tickets half (store/tickets.py) and the runtime half
(store/runtime.py) over two files under one lock (store/files.py). SPEC.md section 8."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from ..config import Project
from ..events import Events
from ..measures import Measures
from .files import locked
from .runtime import Runtime
from .tickets import Tickets


class LocalStore(Tickets, Runtime):
    def __init__(self, project: Project, events: Events, writer: str | None = None):
        self.project = project
        self.prefix = project.config.ticket_prefix
        self.events = events                     # every change is an observation from this source
        self.writer = writer or events.source    # whose record the metric counters are (measures.py): the gate's store writes the scheduler's
        self.measures = Measures(project)
        project.ensure_dirs()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with locked(self.project.lock_file):
            yield
