"""Tickets, milestones, notes, and the state machine. SPEC.md section 2."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def epoch(iso: str | None) -> float | None:
    """A timestamp written by ``now()`` (or any ISO 8601 one) in seconds, None when unreadable."""
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


class State(StrEnum):
    OPEN = "OPEN"
    BLOCKED = "BLOCKED"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    IN_EXTERNAL_REVIEW = "IN_EXTERNAL_REVIEW"   # external review: handed off for a pull request, which people outside review and merge
    IN_PROGRESS = "IN_PROGRESS"
    IN_REVIEW = "IN_REVIEW"
    MERGING = "MERGING"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


ACTIVE = {State.IN_PROGRESS, State.IN_REVIEW, State.MERGING}
TERMINAL = {State.DONE, State.CANCELLED}

# Only a waiting state may be cancelled: an active ticket is held by a machine (or a gate run),
# and cancelling it underneath the holder would leave an assignment on a terminal ticket. It
# settles first (SPEC.md section 2). With external review (``[gate] mode = "external"``) a
# ticket's pull request merged on the other side is final, whatever state the ticket waits in:
# DONE from any waiting state (an active one is released first; tasking/external.py).
TRANSITIONS: dict[State, set[State]] = {
    State.OPEN: {State.IN_PROGRESS, State.CANCELLED, State.DONE},
    State.IN_PROGRESS: {State.READY_FOR_REVIEW, State.BLOCKED, State.OPEN},
    State.READY_FOR_REVIEW: {State.IN_REVIEW, State.REJECTED, State.CANCELLED, State.DONE},   # REJECTED: its folder or worktree is missing at review time
    State.IN_REVIEW: {State.APPROVED, State.REJECTED, State.READY_FOR_REVIEW},
    State.REJECTED: {State.IN_PROGRESS, State.CANCELLED, State.DONE},
    State.APPROVED: {State.MERGING, State.DONE, State.CANCELLED, State.IN_EXTERNAL_REVIEW},   # DONE directly when nothing is merged (work = "tasks")
    State.IN_EXTERNAL_REVIEW: {State.REJECTED, State.DONE, State.BLOCKED, State.CANCELLED},
    State.MERGING: {State.DONE, State.REJECTED, State.APPROVED},
    State.BLOCKED: {State.OPEN, State.CANCELLED, State.IN_EXTERNAL_REVIEW, State.DONE},   # IN_EXTERNAL_REVIEW: its closed pull request reopened
    State.DONE: set(),
    State.CANCELLED: set(),
}

# The waiting state an active state falls back to on crash recovery.
RECOVERY = {
    State.IN_PROGRESS: State.OPEN,
    State.IN_REVIEW: State.READY_FOR_REVIEW,
    State.MERGING: State.APPROVED,
}


class TransitionError(ValueError):
    pass


def check_transition(src: State, dst: State) -> None:
    if dst not in TRANSITIONS[src]:
        raise TransitionError(f"cannot move {src} -> {dst}")


class MilestoneStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass
class Note:
    author: str  # worker | reviewer | merge_gate | task_scheduler (the task system itself) | a planner's role | who acted on its pull request
    text: str
    kind: str = "note"  # note | review | gate | blocker | crash
    created_at: str = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Milestone:
    id: str
    name: str
    description: str = ""
    order: int = 0
    status: MilestoneStatus = MilestoneStatus.OPEN
    version: int = 1
    created_at: str = field(default_factory=now)
    closed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Milestone:
        d = dict(d)
        d["status"] = MilestoneStatus(d.get("status", "open"))
        return cls(**d)


@dataclass
class Ticket:
    id: str
    title: str
    milestone_id: str
    description: str = ""
    acceptance: str = ""
    objective: str = ""      # why: which project objective this serves
    verification: str = ""   # how the work is verified (in code mode, the gate's acceptance tests)
    state: State = State.OPEN
    priority: int = 5
    depends_on: list[str] = field(default_factory=list)
    branch: str | None = None
    owner: str | None = None        # the machine instance working it now (worker/1, reviewer/2); set by the scheduler
    reviewed_commit: str | None = None
    worker: str | None = None       # the worker instance that last worked it: rejected work returns to it (SPEC.md section 3)
    test_output: str | None = None  # code: the worker's run of the unit command on reviewed_commit, for a review offered from the queue
    caused_by: str | None = None    # the id of the event that caused its last transition (CloudEvents causationid)
    blocked_reason: str | None = None
    feedback: str | None = None
    rejected_by: str | None = None
    review_rounds: int = 0        # drives the repeated-rejection escalation
    worker_turns: int = 0         # times a worker took this ticket
    reviewer_rejections: int = 0  # how many of those turns followed a reviewer rejection
    gate_rejections: int = 0      # how many followed a merge-gate send-back (test failure or conflict)
    pull_rejections: int = 0      # how many followed feedback on its pull request (external review)
    pull: str | None = None       # external review: its pull request, ``owner/repo#n``, once linked
    pull_head: str | None = None  # the commit its pull request carries, or is about to: what feedback must be about to count
    pull_state: str | None = None  # open | closed | merged, as the bridge last said
    created_by: str = ""         # the role that asked for it
    version: int = 1
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    state_since: str = field(default_factory=now)

    @property
    def slug(self) -> str:
        base = "".join(c if c.isalnum() else " " for c in self.title.lower()).split()
        return "-".join(base)[:40].strip("-") or "ticket"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Ticket:
        d = dict(d)
        d["state"] = State(d.get("state", "OPEN"))
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


def is_ready(t: Ticket, tickets: dict[str, Ticket], milestones: dict[str, Milestone]) -> bool:
    if t.state != State.OPEN:
        return False
    ms = milestones.get(t.milestone_id)
    if ms is None or ms.status != MilestoneStatus.OPEN:
        return False
    return all(d in tickets and tickets[d].state == State.DONE for d in t.depends_on)


def effective_priorities(tickets: dict[str, Ticket], aging_days_per_level: float = 0.0) -> dict[str, dict]:
    """Effective priority for pickup ordering. Lower is sooner.

    1. A ticket inherits the best priority of anything that (transitively) depends on it,
       so prioritizing a ticket pulls its whole prerequisite chain forward.
    2. Age improves priority by one level per `aging_days_per_level` days since creation
       (0 disables), so old work is finished or cancelled instead of forgotten.
    Returns {id: {"effective": int, "own": int, "inherited_from": id|None, "age_levels": int}}.
    """
    now_ts = time.time()
    live = {tid: t for tid, t in tickets.items() if t.state not in TERMINAL}
    best: dict[str, tuple[int, str | None]] = {tid: (t.priority, None) for tid, t in live.items()}
    # Propagate along reversed dependency edges until stable (graphs are small).
    changed = True
    while changed:
        changed = False
        for tid, t in live.items():
            for dep in t.depends_on:
                if dep in live and best[tid][0] < best[dep][0]:
                    best[dep] = (best[tid][0], best[tid][1] or tid)
                    changed = True
    out: dict[str, dict] = {}
    for tid, t in live.items():
        eff, src = best[tid]
        levels = 0
        created = epoch(t.created_at)                   # an unreadable created_at: no aging
        if aging_days_per_level and aging_days_per_level > 0 and created is not None:
            levels = int((now_ts - created) / 86400 // aging_days_per_level)
        out[tid] = {"effective": max(1, eff - levels), "own": t.priority, "inherited_from": src, "age_levels": levels}
    return out
