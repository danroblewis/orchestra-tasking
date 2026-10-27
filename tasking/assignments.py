"""Assignment: how the task system gives a ticket to whoever fills a role, over Orchestra's bus.

The scheduler does not start the machine that does the work. It offers the work to a role
(``worker``, ``reviewer``), and whatever machine serves that role answers: a program, a person's
tool, a machine from another package, anything that speaks this. The ticket is every event's
subject; ``assignment`` is the id that ties an offer to its answers.

| Event | From | Data (the scheduler reads what is marked ✓; the rest is for listeners) |
|---|---|---|
| ``task.assigned`` | task_scheduler | assignment, role, ticket, workspace (a folder; in code mode a git worktree), branch (null in tasks mode), lease_s (the lease: a heartbeat must come at least this often), brief (the ticket's fields: id, title, objective, description, acceptance, verification, feedback, rejected_by, review_rounds, priority, depends_on, branch, worker_turns); a code worker's unit_command; a reviewer's review {summary (the worker's), commit, test_output} (in code mode the scheduler runs the unit tests before offering the review; commit and test_output are null in tasks mode) |
| ``task.accepted`` | the machine instance (worker/1) | assignment ✓, ticket, role; instance ✓ if sent: the holder is the event's CloudEvents source (worker/1), and an instance that says otherwise is refused (``task.released`` naming the source, the machine that sent it; the offer stays open). The first to accept holds it, and the ticket moves to IN_PROGRESS or IN_REVIEW with it as owner (``ticket.transitioned`` with ``owner`` and ``assignment``: the instance's confirmation that it holds that assignment; a machine whose work costs something waits for it); a later one, or an acceptance of an assignment no longer held, is told ``task.released`` naming its instance |
| ``task.working`` | the instance | assignment ✓, ticket, role: a heartbeat; the lease goes on. ``text`` ✓ if sent: what the instance is doing now, in a line, for the Tasks panel. Only from the instance that holds it |
| ``task.reported`` | the instance | assignment ✓, report ✓ (a worker's: state READY_FOR_REVIEW or BLOCKED, summary, blocked_reason; a reviewer's: verdict APPROVED or REJECTED, summary, feedback; null when it ended without one), failed ✓ (true: it went away or its lease ran out, retried up to twice; false with a null report: it ended on purpose without one, and the ticket is BLOCKED for a planner), text ✓ (default "": its last words, kept when it is blocked), commit ✓ (default null; a reviewer's, in code mode: the commit it reviewed, which is the offer's review.commit; a report naming another commit counts as no verdict; the gate lands reviewed_commit as the scheduler recorded it from the worker's report), ticket, role. Only from the instance that holds it; a report the scheduler cannot use counts as none. A worker that cannot act on a rejection's feedback reports BLOCKED instead of redoing the same work: nothing else stops a program worker and a program reviewer arguing forever |
| ``task.released`` | task_scheduler | assignment, ticket, role, reason (nobody filled the role within the lease; no word from the holder for the lease; the operator stopped it; another instance was first; the instance was not the event's source; the ticket moved on meanwhile; the task system restarted or shut down); instance, only when the release is addressed to one instance. The machine stops what it was doing |

``LEASE_S`` and ``HEARTBEAT_S`` are fixed: every offer says ``lease_s``, and a machine that cannot
keep the lease must not accept.

The scheduler applies every ticket transition itself, from these, and owns the leases: an
assignment with no word from its machine for ``LEASE_S`` is released and its ticket goes back to
waiting (no zombies); an offer nobody accepted is dropped and made again every lease, and changed
nothing: a role nobody fills shows as ``task.assigned``/``task.released`` pairs on the bus."""

from __future__ import annotations

import uuid

ASSIGNED, ACCEPTED, WORKING, REPORTED, RELEASED = "task.assigned", "task.accepted", "task.working", "task.reported", "task.released"
LEASE_S = 180.0          # no word from the assignee for this long and the work goes back
HEARTBEAT_S = 30.0       # how often an assignee says it is still working


def new_id(ticket: str, role: str) -> str:
    return f"{ticket}.{role}.{uuid.uuid4().hex[:8]}"


def brief(t) -> dict:
    """What a machine needs to do the ticket, whoever it is."""
    return {k: getattr(t, k, None) for k in ("id", "title", "objective", "description", "acceptance", "verification", "feedback",
                                             "rejected_by", "review_rounds", "priority", "depends_on", "branch", "worker_turns")}
