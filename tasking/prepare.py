"""Workspaces made off the scheduler's loop. In code mode a turn's worktree may take minutes to be
ready: ``git worktree add`` in a large repository, a pool slot reset to the ticket's branch, the
``[tasks] worktree_setup`` command. The scheduler records what it needs synchronously (the
preparation in the record, the pool slot it holds) and leaves the slow part to a thread, one per
preparation; each tick it takes up the ones that finished. A preparation that finishes within the
tick's grace (``GRACE_S``) is offered at once, as before; one that does not leaves its ticket
``PREPARING`` (a worker's turn) until it does. The thread touches only the disk: what it found is
recorded by the loop. SPEC.md sections 2 and 4."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .model import State

GRACE_S = 2.0          # a tick waits at most this long, in all, for its preparations to finish
STOP_WAIT_S = 10.0     # on shutdown, how long the threads under way are waited for (their setups are ended first)


@dataclass
class Prep:
    """One workspace being made for an offer: ``work`` runs in the thread and returns the
    workspace and branch, or raises."""
    aid: str                                   # the assignment id the offer will have
    ticket: str
    role: str
    origin: State                              # the state the ticket waited in
    work: Callable[[], tuple[Path, str | None]]
    offer: dict = field(default_factory=dict)  # what the offer is made with (type, source, cause, extra)
    slot: dict | None = None                   # the pool slot it holds (tasking/pool.py), if any
    own: bool = False                          # the ticket's own worktree (no pool): recorded when made
    moved: bool = False                        # the ticket was moved to PREPARING
    version: int | None = None                 # its version before it was
    abandoned: str | None = None               # why its result is not to be offered
    started: float = field(default_factory=time.time)
    result: tuple[Path, str | None] | None = None
    error: Exception | None = None
    thread: threading.Thread | None = None

    def run(self) -> None:
        try:
            self.result = self.work()
        except Exception as e:  # noqa: BLE001 - whatever went wrong is the loop's to record
            self.error = e

    @property
    def done(self) -> bool:
        return self.thread is not None and not self.thread.is_alive()


class Preparations:
    """The preparations under way, by assignment id."""

    def __init__(self) -> None:
        self.running: dict[str, Prep] = {}
        self.grace_until: float | None = None  # this tick's deadline (``tick``); None outside a tick

    def start(self, prep: Prep) -> bool:
        """The thread started and waited for within the grace; whether it finished."""
        prep.thread = threading.Thread(target=prep.run, daemon=True, name=f"prepare-{prep.aid}")
        self.running[prep.aid] = prep
        prep.thread.start()
        until = self.grace_until if self.grace_until is not None else time.monotonic() + GRACE_S
        prep.thread.join(max(0.0, until - time.monotonic()))
        return prep.done

    def tick(self) -> None:
        self.grace_until = time.monotonic() + GRACE_S

    def finished(self) -> list[Prep]:
        return [p for p in self.running.values() if p.done]

    def of(self, ticket: str) -> list[Prep]:
        return [p for p in self.running.values() if p.ticket == ticket]

    def busy(self, role: str) -> int:
        return sum(1 for p in self.running.values() if p.role == role)

    def tickets(self) -> set[str]:
        return {p.ticket for p in self.running.values()}

    def stop(self, timeout: float = STOP_WAIT_S) -> None:
        """Each one abandoned, and its thread waited for a while (its setup was ended first)."""
        end = time.monotonic() + timeout
        for p in list(self.running.values()):
            p.abandoned = p.abandoned or "the task system shut down"
            if p.thread is not None:
                p.thread.join(max(0.0, end - time.monotonic()))
        self.running.clear()
