"""Assignment: how the task system gives a ticket to whoever fills a role, over Orchestra's bus.

The scheduler does not start the machine that does the work. It offers the work to a role, and
whatever machine serves that role answers (orchestra-agents' AI agents, a script, anything that
speaks this): docs/proposals/agents-split.md.

| Event | From | Data |
|---|---|---|
| ``task.assigned`` | task_scheduler | assignment, role, ticket, kind (work or review), workspace, branch, lease_s, the brief (the ticket's fields); a code worker's unit_command; a reviewer's review (the worker's summary; in code mode the commit and the unit tests' output, run by the scheduler) |
| ``task.accepted`` | the machine instance (worker/1) | assignment, instance, session |
| ``task.working`` | the instance | assignment (a heartbeat: the lease goes on) |
| ``task.reported`` | the instance | assignment, report (a worker's: state READY_FOR_REVIEW or BLOCKED with a summary or reason; a reviewer's: verdict APPROVED or REJECTED with feedback; none when it ended without one), failed, text |
| ``task.released`` | task_scheduler | assignment, reason (the lease ran out; the operator stopped it) |

The scheduler applies every ticket transition itself, from these, and owns the leases: an
assignment with no word from its machine for ``LEASE_S`` is released and its ticket goes back to
waiting (no zombies). The ticket is every event's subject."""

from __future__ import annotations

import uuid

ASSIGNED, ACCEPTED, WORKING, REPORTED, RELEASED = "task.assigned", "task.accepted", "task.working", "task.reported", "task.released"
LEASE_S = 180.0          # no word from the assignee for this long and the work goes back
HEARTBEAT_S = 30.0       # how often an assignee says it is still working


def new_id(ticket: str, role: str) -> str:
    return f"{ticket}.{role}.{uuid.uuid4().hex[:8]}"


class Follower:
    """Every event on the bus after an offset, in order (a bus read returns the last ``limit``
    events after ``since``, so it is asked for all of them)."""

    def __init__(self, events, start: int | None = None):
        self.events = events
        self.offset = events.end() if start is None else start

    def new(self) -> list[dict]:
        end = self.events.end()
        if end <= self.offset:
            return []
        evs, self.offset = self.events.read(self.offset, limit=end - self.offset)
        return evs


def brief(t) -> dict:
    """What a machine needs to do the ticket, whoever it is."""
    return {k: getattr(t, k, None) for k in ("id", "title", "objective", "description", "acceptance", "verification", "feedback",
                                             "rejected_by", "review_rounds", "priority", "milestone_id", "depends_on", "branch",
                                             "reviewed_commit", "worker_turns")}
