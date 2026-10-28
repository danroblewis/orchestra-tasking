"""Assignment: how work passes between the machines that do it, over Orchestra's bus, and what
the task system reads of it (SPEC.md section 3; the owner's decision D17).

The task system does not start the machines that do the work, and it does not relay between
them. Its scheduler is no machine: it stands in for each role's own queue, so what it publishes
here goes out as that role's machine (``worker``, ``reviewer``), never as ``task_scheduler``
(tasking/events.py), and nothing reads an offer's or a release's source: a claim is decided by
the bus's order (``holder``). It offers waiting work to a role; a worker that has finished asks the reviewers directly; a
reviewer that rejects asks the worker that did the work directly; a reviewer that approves tells
the gate. The task system reads all of it, records it, and takes back what a silent machine
holds. The ticket is every event's subject; ``assignment`` is the id of one turn of work.

**Offers**: an event that offers a turn. All three share these fields: assignment (the turn's id,
made by whoever offers it: ``new_id``), role, ticket, workspace (a folder; in code mode a git
worktree), branch and base (code mode; null in tasks mode), lease_s, brief (``brief``), in code
mode unit_command, a reviewer's review {summary, commit, test_output, worker}, and to (an
instance) when only that instance may take it.

| Event | From | For | Also |
|---|---|---|---|
| ``task.assigned`` | the role's queue, published as the role's machine (``worker``, ``reviewer``) | its role; or ``to``: rejected work returned to the worker that did it | |
| ``review.requested`` | the worker instance holding the work | the reviewer role | ends ✓ (the worker's assignment, which this ends), review.commit ✓ (code: the branch head, beyond the base), review.summary ✓, review.test_output ✓ (the worker's run of unit_command) |
| ``changes.requested`` | the reviewer instance holding the review | to ✓: the worker instance that did the work | ends ✓, feedback ✓, summary ✓, commit ✓ (the commit reviewed; another than the recorded one is no verdict); brief.feedback and brief.rejected_by set |
| ``changes.requested`` (external review) | whoever gave feedback on the ticket's pull request (a person or bot the bridge names), published for them by the task system | to: the worker instance that did the work | no ends (it ends no turn of the protocol: the ticket waited in ``IN_EXTERNAL_REVIEW``); feedback, summary, commit (the head the feedback is about), pull (``owner/repo#n``), pull_feedback {pull, head, reviews, comments, labels, checks, conversation}: metadata only; brief.feedback, brief.rejected_by (that machine) and brief.pull set |

**Claims, work and ends** (the task system reads what is marked ✓):

| Event | From | Data |
|---|---|---|
| ``task.accepted`` | the instance | assignment ✓, ticket, role, instance ✓ if sent. The first valid claim of an offer on the bus holds it: valid when instance, if sent, is the event's source, and when the offer names ``to``, the source is that instance. The claimant reads the bus from the offer to its own claim to know (``holder``); the task system records the same holder and says nothing to a loser |
| ``task.working`` | the holder | assignment ✓, ticket, role, text ✓ if sent: what it is doing now, in a line. At least every HEARTBEAT_S |
| ``review.approved`` | the reviewer holding the review | ends ✓, ticket, role, summary ✓, commit ✓. For the gate (merge_gate) in code mode; done in tasks mode |
| ``task.reported`` | the holder | assignment ✓, report ✓ (a worker's: state BLOCKED with blocked_reason, summary; null when the turn ended without one), failed ✓ (true: it went away or ran out its lease), text ✓ (its last words), commit, ticket, role. Work that passes to no machine: READY_FOR_REVIEW or a verdict here is unusable |
| ``task.released`` | the role's queue, as ``worker`` or ``reviewer`` | assignment, ticket, role, reason; instance, when addressed to one. An offer nobody claimed within its lease (or an addressed one within the return timeout), a holder silent for the lease, the operator, a restart or shutdown, or a claim the task system cannot record (not held, the ticket moved on, an instance that is not the source). The machine stops |

Every event a machine publishes in a turn names the offer that opened it as its CloudEvents
``causationid``; an offer that ends a turn names the offer of the turn it ends.

``LEASE_S`` and ``HEARTBEAT_S`` are fixed: every offer says ``lease_s``, and a machine that
cannot keep the lease must not take it. ``RETURN_S`` is the default of ``[tasks]
return_timeout``: how long rejected work waits for the worker it is addressed to."""

from __future__ import annotations

import uuid

ASSIGNED, ACCEPTED, WORKING, REPORTED, RELEASED = "task.assigned", "task.accepted", "task.working", "task.reported", "task.released"
REVIEW_REQUESTED, CHANGES_REQUESTED, APPROVED = "review.requested", "changes.requested", "review.approved"
OFFERS = (ASSIGNED, REVIEW_REQUESTED, CHANGES_REQUESTED)     # the events that offer a turn
LEASE_S = 180.0          # no word from the assignee for this long and the work goes back
HEARTBEAT_S = 30.0       # how often an assignee says it is still working
RETURN_S = 60.0          # rejected work waits this long for the worker it is addressed to ([tasks] return_timeout)


def new_id(ticket: str, role: str) -> str:
    return f"{ticket}.{role}.{uuid.uuid4().hex[:8]}"


def brief(t) -> dict:
    """What a machine needs to do the ticket, whoever it is."""
    return {k: getattr(t, k, None) for k in ("id", "title", "objective", "description", "acceptance", "verification", "feedback",
                                             "rejected_by", "review_rounds", "priority", "depends_on", "branch", "worker_turns", "pull")}


def valid_claim(ev: dict, offer: dict) -> bool:
    """A claim (``task.accepted``, flat) that may hold ``offer``: its instance, if sent, is its
    source, and an offer addressed ``to`` one instance is claimed by that instance."""
    src = ev.get("source")
    return (ev.get("type") == ACCEPTED and ev.get("assignment") == offer.get("assignment") and bool(src)
            and ev.get("instance") in (None, src) and offer.get("to") in (None, src))


def holder(evs: list[dict], offer: dict) -> str | None:
    """Who holds ``offer`` by the bus's order: the source of its first valid claim among ``evs``
    (flat events, in bus order, read from the offer on), or None while nobody has claimed it."""
    return next((ev["source"] for ev in evs if valid_claim(ev, offer)), None)


def pass_on(offer: dict, type: str, *, source: str, **fields) -> dict:
    """The data of an offer that ends the turn ``offer`` opened and offers the next one: the same
    ticket, workspace, branch, base, lease and unit command, a new assignment for the next role
    (a review for ``review.requested``, the worker's for ``changes.requested``), ``ends`` naming
    the turn it ends, and ``fields`` on top. For machines that may import this package (its own
    tests); others write the same few lines (demo/machines/)."""
    role = "reviewer" if type == REVIEW_REQUESTED else "worker"
    keep = {k: offer.get(k) for k in ("ticket", "workspace", "branch", "base", "lease_s", "unit_command") if k in offer}
    out = {**keep, "assignment": new_id(offer["ticket"], role), "role": role, "ends": offer["assignment"],
           "brief": dict(offer.get("brief") or {})}
    if type == CHANGES_REQUESTED:
        out["to"] = (offer.get("review") or {}).get("worker")
        b = out["brief"]                          # as the ticket will read once the worker takes it back
        b.update(feedback=fields.get("feedback"), rejected_by=source.partition("/")[0],
                 worker_turns=(b.get("worker_turns") or 0) + 1, review_rounds=(b.get("review_rounds") or 0) + 1)
    return {**out, **fields}
