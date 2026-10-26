"""Tickets, milestones, notes, and the state machine. See SPEC.md section 3."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class State(StrEnum):
    OPEN = "OPEN"
    BLOCKED = "BLOCKED"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    IN_PROGRESS = "IN_PROGRESS"
    IN_REVIEW = "IN_REVIEW"
    MERGING = "MERGING"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


WAITING = {State.OPEN, State.BLOCKED, State.READY_FOR_REVIEW, State.REJECTED, State.APPROVED}
ACTIVE = {State.IN_PROGRESS, State.IN_REVIEW, State.MERGING}
TERMINAL = {State.DONE, State.CANCELLED}

# Board column order.
STATE_ORDER = [
    State.OPEN, State.BLOCKED, State.IN_PROGRESS, State.REJECTED, State.READY_FOR_REVIEW,
    State.IN_REVIEW, State.APPROVED, State.MERGING, State.DONE, State.CANCELLED,
]

TRANSITIONS: dict[State, set[State]] = {
    State.OPEN: {State.IN_PROGRESS, State.CANCELLED},
    State.IN_PROGRESS: {State.READY_FOR_REVIEW, State.BLOCKED, State.OPEN, State.CANCELLED},
    State.READY_FOR_REVIEW: {State.IN_REVIEW, State.CANCELLED},
    State.IN_REVIEW: {State.APPROVED, State.REJECTED, State.READY_FOR_REVIEW, State.CANCELLED},
    State.REJECTED: {State.IN_PROGRESS, State.CANCELLED},
    State.APPROVED: {State.MERGING, State.CANCELLED},
    State.MERGING: {State.DONE, State.REJECTED, State.APPROVED, State.CANCELLED},
    State.BLOCKED: {State.OPEN, State.CANCELLED},
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
    author: str  # worker | reviewer | merge_gate | supervisor | coordinator | orchestra
    text: str
    kind: str = "note"  # note | review | gate | blocker
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
    verification: str = ""   # how the merge gate's acceptance tests prove it
    state: State = State.OPEN
    priority: int = 5
    depends_on: list[str] = field(default_factory=list)
    branch: str | None = None
    worker_session: str | None = None
    reviewer_session: str | None = None
    reviewed_commit: str | None = None
    blocked_reason: str | None = None
    feedback: str | None = None
    rejected_by: str | None = None
    review_rounds: int = 0        # drives the repeated-rejection escalation
    worker_turns: int = 0         # times a worker was opened for this ticket (re-engagements = turns - 1)
    reviewer_rejections: int = 0  # how many of those turns followed a reviewer rejection
    gate_rejections: int = 0      # how many followed a merge-gate send-back (test failure or conflict)
    created_by: str = "supervisor"
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


def effective_priorities(tickets: dict[str, "Ticket"], aging_days_per_level: float = 0.0,
                         at: float | None = None) -> dict[str, dict]:
    """Effective priority for pickup ordering. Lower is sooner.

    1. A ticket inherits the best priority of anything that (transitively) depends on it,
       so prioritizing a ticket pulls its whole prerequisite chain forward.
    2. Age improves priority by one level per `aging_days_per_level` days since creation
       (0 disables), so old work is finished or cancelled instead of forgotten.
    Returns {id: {"effective": int, "own": int, "inherited_from": id|None, "age_levels": int}}.
    """
    import time as _time
    from datetime import datetime
    now_ts = at or _time.time()
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
        if aging_days_per_level and aging_days_per_level > 0:
            try:
                age_days = (now_ts - datetime.fromisoformat(t.created_at).timestamp()) / 86400
                levels = int(age_days // aging_days_per_level)
            except Exception:
                levels = 0
        out[tid] = {"effective": max(1, eff - levels), "own": t.priority, "inherited_from": src, "age_levels": levels}
    return out
