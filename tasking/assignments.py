"""Assignment: how the task system gives a ticket to whoever fills a role, over Orchestra's bus.

The scheduler does not start the machine that does the work. It offers the work to a role
(``worker``, ``reviewer``), and whatever machine serves that role answers: a program, a person's
tool, a machine from another package, anything that speaks this. The ticket is every event's
subject; ``assignment`` is the id that ties an offer to its answers.

| Event | From | Data (the scheduler reads what is marked ✓; the rest is for listeners) |
|---|---|---|
| ``task.assigned`` | task_scheduler | assignment, role, ticket, kind (work or review), workspace (a folder; in code mode a git worktree), branch (null in tasks mode), lease_s (the lease: a heartbeat must come at least this often), brief (the ticket's fields: id, title, objective, description, acceptance, verification, feedback, rejected_by, review_rounds, priority, milestone_id, depends_on, branch, reviewed_commit, worker_turns); a code worker's unit_command; a reviewer's review {summary (the worker's), commit, test_output} (in code mode the scheduler runs the unit tests before offering the review) |
| ``task.accepted`` | the machine instance (worker/1) | assignment ✓, instance ✓, ticket, role. The first to accept holds it; a later one is told ``task.released`` naming its instance |
| ``task.working`` | the instance | assignment ✓, ticket, role: a heartbeat; the lease goes on. Only from the instance that holds it |
| ``task.reported`` | the instance | assignment ✓, report ✓ (a worker's: state READY_FOR_REVIEW or BLOCKED, summary, blocked_reason; a reviewer's: verdict APPROVED or REJECTED, summary, feedback; null when it ended without one), failed ✓ (default false: it went away or ran out of time), text ✓ (default "": its last words, kept when it is blocked), commit ✓ (default null; a reviewer's, in code mode), ticket, role. Only from the instance that holds it; a report the scheduler cannot use counts as none |
| ``task.released`` | task_scheduler | assignment, ticket, role, reason (nobody filled the role within the lease; no word from the holder for the lease; the operator stopped it; another instance was first; the task system restarted or shut down); instance, only when the release is addressed to one instance because another was first. The machine stops what it was doing |

``LEASE_S`` and ``HEARTBEAT_S`` are fixed: every offer says ``lease_s``, and a machine that cannot
keep the lease must not accept.

The scheduler applies every ticket transition itself, from these, and owns the leases: an
assignment with no word from its machine for ``LEASE_S`` is released and its ticket goes back to
waiting (no zombies); an offer nobody accepted is offered again and counted against no one."""

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
