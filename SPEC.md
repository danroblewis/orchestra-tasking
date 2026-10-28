# The task system

A task system observed by Orchestra. It holds tickets and milestones, decides which ticket is
worked next and by which role, offers that work over Orchestra's event bus, applies what comes
back, and in code mode lands approved branches through a merge gate. It is an Orchestra plugin
(`tasks`): its pages and API live inside Orchestra's web UI.

It runs no machine of its own besides the merge gate (`merge_gate`, code mode), which lands
approved branches or, with external review, hands them off for a pull request that people outside
review and merge (section 5). Its scheduler is
a daemon, not a machine: it stands in for each machine's own scheduling and publishes only as the
machine it acts for (section 3, "Who publishes what"). Whoever fills the `worker` and `reviewer`
roles is another program that speaks the contract in section 3: in this repository's demo, a
shell runner and a file checker (`demo/`); elsewhere, anything else. This file is the design;
`AGENTS.md` is the short list of rules that must not be broken.

## 1. What it is, and is not

- It knows **tickets, milestones, roles, capacity, leases, workspaces, the gate**. A role is a
  name with a capacity (how many tickets may be offered to it at once) and a lease.
- It does **not** know what a machine is made of. It reads an acceptance for the instance that
  holds the work and a report for its verdict, nothing more. What a machine is made of is not
  here; a package that runs machines of its own kind is a machine like any other and imports
  nothing from this one.
- **Only the scheduler writes the record.** The machines that do the work pass it between them
  directly (section 3), and the scheduler records each step they take; a planner's request
  (section 3) it applies itself. It is the record and the keeper of leases, not a relay. A planner is whatever decides what the work is: a person's script, a
  program, an agent in another package. The planner roles the task system names in its
  attention events are `supervisor` and `coordinator`; they are names, not programs here.
- Every **crossing is on the bus**: every offer, claim, hand-off, report, request and answer is
  a CloudEvent, published by the machine whose act it is. The ticket is the subject of every act
  on it, so Orchestra's Machines page shows who passed what to whom without reading any payload.
  **The record is the store**: what the scheduler records of a step another machine announced (a
  ticket's state, its notes) is not published again; it is read from the record (the queries,
  the Tickets page) and measured (section 8).

## 2. Tickets and milestones (`tasking/model.py`, `tasking/store/`)

A **milestone** (`M1`, `M2`, …) has a name, a description, an order and a status, `open` or
`closed`. Every ticket belongs to one. A milestone closes only when all its tickets are `DONE`
or `CANCELLED`, never reopens, and never receives tickets again; the store enforces this.

A **ticket** (`<prefix>-<n>`) has a title, a description (what to do), an acceptance text (how
the reviewer tells it is done), an objective (why), a verification text (how the gate's tests
prove it), a priority (1 is soonest; default 5), dependencies (`depends_on`), and the fields the
scheduler keeps: `state`, `owner` (the machine instance that holds it now), `branch` and
`reviewed_commit` (code mode), `blocked_reason`, `feedback` and `rejected_by` (what the next
worker is told), `review_rounds`, `worker_turns`, `reviewer_rejections`, `gate_rejections`,
with external review `pull` (its pull request, `owner/repo#n`), `pull_head` (the commit the pull
request carries, or is about to), `pull_state` (`open`, `closed`, `merged`) and
`pull_rejections`, `created_by` (the role that asked for it), a `version` for optimistic concurrency, and
timestamps. **Notes** record what happened: the worker's summary, the reviewer's verdict, the
gate's result, a blocker, a crash, a planner's note; with external review, what happened on its
pull request, signed by the machine the bridge named for it (kind `pull`).

**Readiness** is derived: a ticket is ready when it is `OPEN`, every ticket it depends on is
`DONE`, and its milestone is open. **Effective priority** (`effective_priorities`) is the
ticket's own, improved by the best priority of anything that depends on it (so prioritising a
ticket pulls its prerequisites forward) and by one level per `aging_days_per_level` days of age.

### States

| Group | State | Meaning |
|---|---|---|
| waiting | `OPEN` | waits for a worker |
| | `BLOCKED` | a worker could not go on; `blocked_reason` says why; a planner decides (unblock, change, cancel) |
| | `READY_FOR_REVIEW` | waits for a reviewer |
| | `REJECTED` | a reviewer or the gate sent it back; waits for a worker, who is given `feedback` |
| | `APPROVED` | approved; in code mode waits for the gate |
| | `IN_EXTERNAL_REVIEW` | external review: handed off for a pull request; waits for the people and checks on it, and for its merge |
| active | `IN_PROGRESS` | held by a worker |
| | `IN_REVIEW` | held by a reviewer |
| | `MERGING` | in a gate run (code mode) |
| terminal | `DONE` | approved and, in code mode, landed on the base branch; with external review, its pull request merged |
| | `CANCELLED` | a planner cancelled it |

### Transitions (`TRANSITIONS` in `model.py`)

| From | To | Cause |
|---|---|---|
| `OPEN`, `REJECTED` | `IN_PROGRESS` | a worker's claim is the first on the bus: of the queue's offer, or of changes requested of it (`worker_turns` + 1; `worker` = the instance) |
| `IN_PROGRESS` | `READY_FOR_REVIEW` | the worker asks the reviewers (`review.requested`); in code mode the commit it names, on the branch, becomes `reviewed_commit` |
| `IN_PROGRESS` | `BLOCKED` | the worker reports it with a reason; or the worker went away three times in a row; or in code mode the commit it asks a review of has nothing beyond the base |
| `IN_PROGRESS` | `OPEN` | the worker went away (lease ran out, no report, or a report or review request the task system cannot use), the operator stopped it, or the scheduler restarted or shut down: offered again |
| `READY_FOR_REVIEW` | `IN_REVIEW` | a reviewer's claim is the first on the bus: of the worker's request, or of the queue's offer |
| `READY_FOR_REVIEW` | `REJECTED` | the ticket's folder or worktree is missing at review time (`rejected_by = task_scheduler`; not a review round) |
| `IN_REVIEW` | `APPROVED` | the reviewer approves (`review.approved`) |
| `IN_REVIEW` | `REJECTED` | the reviewer asks the worker for changes (`changes.requested`, `review_rounds` + 1); or four reviews in a row ended without a verdict (three were redone) |
| `IN_REVIEW` | `READY_FOR_REVIEW` | the reviewer went away or was stopped: reviewed again |
| `APPROVED` | `DONE` | tasks mode: nothing to merge |
| `APPROVED` | `MERGING` | code mode: the gate (`merge_gate`) takes it |
| `MERGING` | `DONE` | the gate landed it |
| `MERGING` | `REJECTED` | the gate's tests failed (`review_rounds` + 1), or the branch conflicts (not a review round); `rejected_by = merge_gate` |
| `MERGING` | `APPROVED` | the base branch itself is broken, or the gate hit an error: nothing attributed, the pipeline pauses |
| `BLOCKED` | `OPEN` | a planner unblocks it |
| any waiting state | `CANCELLED` | a planner cancels it (an active ticket must settle first) |
| `APPROVED` | `IN_EXTERNAL_REVIEW` | external review: the gate hands the branch off for its pull request (`gate.handed_off`) |
| `IN_EXTERNAL_REVIEW` | `REJECTED` | feedback on the pull request's head (changes requested, line comments, a failing label or check): back to the worker that did it (`review_rounds` + 1, `pull_rejections` + 1; `rejected_by` = whoever gave it) |
| `IN_EXTERNAL_REVIEW` | `BLOCKED` | the pull request was closed without a merge (`blocked_reason` says by whom) |
| `BLOCKED` | `IN_EXTERNAL_REVIEW` | that pull request was reopened |
| any waiting state | `DONE` | external review: its pull request was merged (whoever held it is released first) |

An offer nobody claims is only a released assignment: no transition, nothing counted. When
`review_rounds` reaches `review_rounds_before_escalation` (and every multiple of it), whether a
reviewer, the task system or the gate sent it back (`reports.send_back`, the one place a
rejection is counted), the coordinator role is asked to decide; nothing fails automatically. On start and on shutdown the
scheduler tells whoever holds work to stop (`task.released`), drops every assignment and moves
every active ticket to the waiting state before it, with a note: no zombies.

## 3. The contract on the bus

Everything crosses Orchestra's bus as CloudEvents (`tasking/events.py` over
`orchestra.events`). The ticket is the `subject`; the `source` is the machine (or
`<machine>/<instance>`). The scheduler is no machine and never a source: what it publishes goes
out as the machine whose responsibility it implements at that moment (`coordinator/dispatch` for
the queue, `worker` or `reviewer` for a held turn taken back, `merge_gate` for the gate, the asker
for a request's answer, `probes` for the `[[probes]]`), and `tasking/events.py` refuses a publish
as `task_scheduler`. The other sources
the task system publishes as are `task_web` (the operator's controls in the web UI) and `task_cli`
(`tasking staging deploy`); `tasking api` publishes as the role it is asked to be.

**Who speaks (owner's decision D17, 2026-10-27, refined the same day: the scheduler is no
machine).** Work passes directly between the machines that do it, and the task system listens to
all of it and records it. It is the record (tickets, states, history), the keeper of leases (work
held by a silent machine is taken back: no zombies), the keeper of capacity, the one that offers
waiting work, and the merge gate. It is not a relay between the machines doing the work. The
scheduler is one daemon standing in for each machine's own scheduler (the coordinator's dispatch,
the leases of the worker's and the reviewer's turns, the merge gate, the answers to a planner's
requests); one daemon per role would do the same, and one is simpler.

**Dispatch is the coordinator's** (the owner, 2026-10-27: "when the task_scheduler first assigns a
ticket to a worker, it should probably be an event from the coordinator to the worker"). Choosing
the next ticket to offer (readiness, priority, capacity, when an offer nobody took goes out again)
is dispatch, the coordinator role's responsibility, so the queue publishes as an instance of the
coordinator machine, `coordinator/dispatch`, beside whatever else fills that role (the agents'
Coordinator, an AI agent, announces as `coordinator`). Orchestra shows one `coordinator` machine:
what it listens for is the union over its instances, its kind says both (`agent ×1, program ×1`),
and each instance's card is its own, so the dispatch's announcement erases nothing the agent said
of itself (Orchestra's `web/machines.py`). Rejected work still returns to the worker that did it: a reviewer's rejection
directly (`changes.requested`, reviewer → worker: D17); a gate rejection through dispatch
(merge_gate → coordinator → worker). The gate is a program that merges and says why it could not
(`gate.rejected`); it holds no turn and offers none, and returning the work to the worker that did
it, or to any worker after the return timeout, is dispatch's decision, the same one it makes for a
reviewer's rejection that reached the queue (a restart, the return timeout). It publishes only an act no machine's own process already announced, and
publishes it as that machine. So:

- a worker that has finished asks the reviewers directly (`review.requested`), and a reviewer
  takes the review from the bus;
- a reviewer that rejects asks the worker instance that did the work for changes
  (`changes.requested`, addressed `to` it), and that instance takes the work back in the same
  session; only if it does not take it within the return timeout does the task system put the
  ticket in the queue, and it says so;
- a reviewer that approves says so (`review.approved`), and the task system's gate (`merge_gate`,
  a machine of its own) takes it;
- with external review (section 5), the people and checks on the ticket's pull request review it
  on GitHub, and the pull-request bridge publishes what they did (`github.pr.*`, as the person or
  bot who did it). Feedback there asks the worker that did the work for changes as an internal
  reviewer would: `changes.requested`, addressed `to` that worker, published as whoever gave the
  feedback, with the bridge's event as its cause. Nobody on the pull request speaks this protocol,
  so the scheduler stands in for their side of it, as it stands in for the coordinator's dispatch.
  If that worker does not take it back within the return timeout, dispatch withdraws the offer
  (`task.released`, as `coordinator/dispatch`) and offers the work to any worker (`task.assigned`,
  as `coordinator/dispatch`), as for any rejected work. This
  is the one place where the task system turns another machine's words into a state (its rule 1
  says so): the feedback's words are the bridge's event, and the offer is the reviewer's;
- the task system speaks when a recurring ticket is due (`ticket.created`, as
  `planner/recurring`: section 7), when dispatch offers waiting work (`task.assigned`, as
  `coordinator/dispatch`), takes work back (`task.released`: an offer nobody claimed as
  `coordinator/dispatch`, a held turn as its role), runs the gate (as `merge_gate`), answers a request (as the asker), or asks a planner (`task.attention_needed`, as
  the machine whose work is stuck). The table "Who publishes what" at the end of this section is
  the whole list.

Orchestra's event map infers a hand-off from who acts next on a subject, from the envelope alone,
and names the event it came by from what the receiver listens for (Orchestra's SPEC §2). A
reworked ticket therefore reads planner → coordinator (via `ticket.created`) → worker
(`task.assigned`) → reviewer (`review.requested`) → worker (`changes.requested`) → reviewer →
merge_gate (`review.approved`), with nothing in between; after a gate rejection, merge_gate →
coordinator (`gate.rejected`) → worker (`task.assigned`, addressed to the worker that did it); a
held turn taken back, worker → coordinator (`task.released`) → worker. A ticket the Supervisor
asked for reads supervisor → coordinator (`ticket.created`); a recurring one, planner →
coordinator (`ticket.created`, as `planner/recurring`, section 7). The hand-offs into dispatch rest on
its announcement (**Announcement**, below): those are what make it act. With external review, reviewer → merge_gate (`review.approved`) → the pull request's
reviewer (`gate.handed_off`) → worker (`changes.requested`) → reviewer → merge_gate; when that
worker does not take the work back within the return timeout, the work falls back to the queue
and reads worker → coordinator (the return timeout) → worker (`task.assigned`, as
`coordinator/dispatch`), as after any rejection. Every queue offer is dispatch's; only the pull
request's feedback is published as its reviewer.
`tests/e2e/test_handoffs_e2e.py` and `tests/e2e/test_external_e2e.py` check this, and that no event has the source `task_scheduler`.

**Offers.** Three events offer a turn of work, and share one shape: `assignment` (the turn's id,
`<ticket>.<role>.<8 hex>`, made by whoever offers it), `role`, `ticket`, `workspace` (a folder; in
code mode a git worktree), `branch` and `base` (code mode; null in tasks mode), `lease_s`, `brief`
(the ticket's fields: `assignments.brief`), in code mode `unit_command` (the project's first
`[[gate.unit]]` command), a reviewer's `review` {summary (the worker's), commit, test_output (the
worker's run of the unit command; null in tasks mode), worker (the instance that did the work)},
and `to`, an instance, when the offer is for that instance only.

| Event | From | For | Also carries |
|---|---|---|---|
| `task.assigned` | dispatch, published as `coordinator/dispatch` | its `role`; or `to` when it returns rejected work to the worker that did it | |
| `review.requested` | the worker instance that holds the work | the `reviewer` role | `ends`: the worker's own assignment, which this ends |
| `changes.requested` | the reviewer instance that holds the review | `to`: the worker instance that did the work | `ends`; `feedback`, `summary`, `commit` (the commit reviewed); the brief's `feedback` and `rejected_by` already set |
| `changes.requested` (external review) | whoever gave feedback on the ticket's pull request, published for them by the task system | `to`: the worker instance that did the work | no `ends`; `feedback`, `summary`, `commit` (the head the feedback is about), `pull`, `pull_feedback` (metadata: reviews, line comments' files and lines, labels, checks); the brief's `feedback`, `rejected_by` and `pull` set |

**The end of a turn.** A turn ends with one of: `review.requested` or `changes.requested` (the
work passes on), `review.approved` {ends, ticket, role, summary, commit} (to the gate), or
`task.reported` {assignment, ticket, role, report, failed, text, commit} when the work passes to
no machine: a worker's `{state: BLOCKED, summary, blocked_reason}` (a planner decides), or a null
report (the turn ended without one: `failed` true when the machine went away, false when it ended
on purpose). Only the holder of the assignment it ends may end a turn; anything else is ignored. A
`task.reported` that says `READY_FOR_REVIEW` or carries a verdict is unusable: work passes on with
the events above.

**Claims and races.** A machine that takes an offer publishes `task.accepted` {assignment, ticket,
role, instance?}. The bus is one ordered log that every reader reads in the same order, so the
race is decided by it: **the first valid claim of an offer on the bus holds the turn**. A claim
is valid when its `instance`, if sent, is its own source and, when the offer names `to`, it comes
from that instance. A claimant knows it holds the turn by reading the bus from the offer to its
own claim: no valid claim of that offer before its own. The task system records the same holder
by the same rule and says nothing to a loser, which knows. A machine whose work costs something
(a model's tokens, a build) starts nothing before it has read that, so losing costs nothing.
The task system answers a claim with `task.released` naming its instance only when it cannot
record it: the offer is not held (released, settled, never valid, from before a restart), the
ticket moved on (cancelled meanwhile), or the claim names another instance than its source (the
offer stays open). Such a release can come after the claimant read that it was first, so a
machine stops whatever it started when one names it or its assignment.

**Working.** The holder publishes `task.working` {assignment, text?} at least every `HEARTBEAT_S`
(30 s) while it works: `text`, what it is doing now in a line, is shown on the Tasks panel.

**Timeouts.** `LEASE_S` (180 s, fixed; every offer says its `lease_s`): an offer nobody claims
within it is taken back (`task.released`, no instance, as `coordinator/dispatch`) and the ticket waits in the queue, which
the task system offers again after a pause (`AGAIN_FIRST_S`, 60 s, doubling per offer nobody
took, up to `AGAIN_MAX_S`, 15 min), or at once when the ticket changes or a machine that announces
it listens for offers starts; a held turn with no word from its holder for `LEASE_S` is taken back
as a crash (no zombies). `[tasks] return_timeout` (60 s by default): an offer addressed `to` one
instance that it does not claim within this is taken back with a reason that names the instance
and the wait, the ticket gets a note saying the same, and it is offered to any worker at once.
Orchestra has no `machine.stopped`, so a worker that is gone is known only by this timeout. A
machine accepts only what it can do within the lease.

**Code mode: the tests before a review.** The worker runs `unit_command` on the commit it asks to
have reviewed and puts the output in the request (`review.test_output`), and the reviewer is told
whose run it is. The task system verifies at the gate, where every spec runs on the merge before
anything lands. A machine that passes work on leaves the workspace at the commit it names: its
work committed, nothing untracked (a reviewer removes what its review left), since the next turn
starts there.

**Causation.** Every event a machine publishes in a turn (its claim, heartbeats, the event that
ends it) names the offer that opened the turn as its CloudEvents `causationid`; an offer that ends
a turn names the offer of the turn it ends. What the scheduler publishes names the event it
answers or acts on the same way (`emit(..., cause=id)` in `tasking/events.py`): `gate.started`
the approval, `ticket.created`, `milestone.created`, `request.done` and `request.refused` the
request. Every event about a ticket carries it as its subject. A step another machine announced is
recorded in the store and not published again, so there is no record's announcement to tell apart
from an act. The ticket keeps the id of the event that caused its last transition (`caused_by`),
and the instance that last worked it (`worker`), to which rejected work returns.

The tables in the docstring of `tasking/assignments.py` are the one source of truth for these
events and fields. The agents (orchestra-agents) copy the names by hand and import nothing from
here; their drift test reads this package's source.

**Requests** (`tasking/requests.py`). A planner changes tickets only by asking: `task.requested`
(create), `task.change_requested`, `task.note_requested`, `task.cancel_requested`,
`task.unblock_requested`, `milestone.requested`, `milestone.change_requested`,
`milestone.close_requested`, `capacity.change_requested`, `pipeline.resume_requested`; and reads
with `task.query_requested` naming the `op` (`list_tickets`, `get_ticket`, `project_status`,
`list_milestones`, `get_capacity`, `snapshot`, `list_gate_runs`, `get_gate_run`,
`get_staging_status`, `check_recurring`); a query and its answer carry no subject, since a read acts on no ticket.
A ticket's state and notes are read this way (`get_ticket`, `list_tickets`, `snapshot`): no event
restates them.
Each carries `request` (an id), `args` and `by`: the label the asker
chooses, which names the author of what it makes (a change without one is refused). Which planner
may change what is the planners' own organisation; the task system checks the rules of tickets
and milestones, not who asks. The scheduler's desk applies it with `tasking/api.py` and answers
`request.done` {request, result} or `request.refused` {request, error}, published as the asker
(the request's source) with the request as `causationid`: the desk stands in for the asker's own
request handling. A request that creates something also publishes `ticket.created` or
`milestone.created` as the asker, since the new id is news; any other change (an update, a note,
a cancel, an unblock, a milestone's change or close, the capacity dial, resuming the pipeline) is
answered by `request.done` alone, since the request already said what it asks. `tasking api` sends one
such request from the command line; `requests.ask` does it from Python.

**Attention.** `task.attention_needed` {to, reason, ticket?}, published as the machine whose
work is stuck or whose responsibility raised it (`worker`, `reviewer` or `merge_gate`: "Who
publishes what"), when a planner should decide something, and only then: a ticket `BLOCKED`; a ticket rejected at every multiple
of `review_rounds_before_escalation`; a landing that changed `orchestra.toml` while the
repository root is not on the base branch, so the root's copy did not follow (to the
coordinator); the base branch broken; a gate error; the pipeline still paused after a restart; a
staging deploy failed; staging could not be restarted. `to` is the planner role, `coordinator`
or `supervisor`. Routine creations, completions and landings are not attention:
`milestone.created`, `ticket.created`, `gate.landed` and `request.done` carry them for anyone
listening, and the record has every move. Nothing listens unless a planner machine does.

**Announcement.** Every machine publishes `orchestra.machine.started` when it starts, with its
`mode`, `kind`, what it `listens_for` and where its `log` is; Orchestra's Machines page reads only
those fields, and the rest is passed to the package's card. The scheduler announces nothing of
itself: it is no machine, and that it runs is in the runtime record (its pid) and on Orchestra's
Core page as the process `tasks.scheduler`. It announces, at each start, for the two machines
whose part it implements: dispatch, as `coordinator/dispatch` {mode: continuous, kind: program,
listens_for: [gate.conflict, gate.rejected, task.released, ticket.created], log} (no package: the
coordinator is whoever fills that role; an instance of its own, so its kind and card are its own
and a restart overwrites nothing the agent coordinator announced), so Orchestra names the
hand-offs into dispatch; and in code mode the gate, as `merge_gate` {package: tasks, mode:
continuous, kind: program, listens_for: [review.approved], log}. It announces nothing for the
worker or the reviewer: their instances say what they listen for (`task.assigned`, the offers).

**The names.** `task.*` is the protocol between the task system and the machines that fill roles
(assignment, requests, attention); `ticket.created` and `milestone.created` say what a request
made (its id). So a request to create a ticket is `task.requested` and what it made is
`ticket.created`; `ticket.requested` is a candidate name at the next contract change.

**Observations.** The task system also publishes what happened, for Orchestra and anyone
listening, in families named up to the first dot (Orchestra's event map groups them so):
`ticket.created`, `milestone.created`, `capacity.changed`, `pickup.paused`/`resumed`,
`kill.requested`, `pipeline.paused`, `probe.failed`/`recovered`,
`gate.started`/`conflict`/`landed`/`base_broken`/`rejected`/`error`/`handed_off`,
`staging.deployed`/`failed`/`restarted`. Orchestra never reads their data. These families and the
fields named here are part of what listeners may rely on, like the assignment table and the
request names: `ticket.created` {ticket}, `gate.base_broken` {gate_run}, `probe.failed`/
`probe.recovered` {probe}, `gate.handed_off` {ticket, branch, commit, base, repository, pull}; rename
them deliberately and say so in the commit.

**No echoes** (the owner, 2026-10-27). Every change of a ticket's state is caused by an act some
machine already announced: a claim, `review.requested`, `changes.requested`, `review.approved`,
`task.reported`, a `gate.*` event, `task.released`, or a request answered by `request.done`. An
event that restated it would say twice what happened. So these no longer exist:
`ticket.transitioned`, `ticket.note_added`, `ticket.updated`, `milestone.updated`,
`milestone.closed`, `scheduler.stopped`, and `capacity.changed` and `pipeline.resumed` when a
planner's request caused them (`request.done` answers it). A ticket's state and notes are read
from the record (`get_ticket`, `list_tickets`, `snapshot`, the Tickets page); its moves are
measured by `tasks_ticket_transitions_total` (section 8).

### Who publishes what

What the scheduler does, and the source it publishes it as. It never publishes as
`task_scheduler`.

| What the scheduler does | Published as (source) | Events |
|---|---|---|
| creates a ticket or milestone on a planner's request | the asker (the request's source); `causationid` = the request | `ticket.created`, `milestone.created` |
| answers a request or query | the asker; `causationid` = the request; subject = the ticket for a change, none for a query | `request.done`, `request.refused` |
| applies any other request (update, note, cancel, unblock, milestone change or close, capacity, resume the pipeline) | nothing beyond the answer | none |
| offers waiting work to a worker from the queue: a ticket's first offer from `OPEN`, an offer again after one nobody took or a held turn taken back, rework that reached the queue (also rejected work addressed `to` the worker that did it, after a gate rejection or a restart) | `coordinator/dispatch`: choosing what is offered next (readiness, priority, capacity, when it goes out again) is dispatch, the coordinator's | `task.assigned` |
| offers waiting reviews from the queue: a review nobody took, a reviewer gone, after a restart | `coordinator/dispatch`, for the same reason: which waiting review goes to the reviewers' capacity is dispatch. A worker's `review.requested` stays the worker's own hand-off to the reviewers; the queue offers only what that hand-off did not place | `task.assigned` |
| withdraws an offer nobody claimed: its lease ran out, an addressed one was not taken within the return timeout, the ticket moved on, the operator's stop, a restart or shutdown | `coordinator/dispatch`, whoever made the offer (the queue, a worker's `review.requested`, a reviewer's `changes.requested`): nobody holds it, and it goes back into dispatch's queue, so no role is drawn as having had it | `task.released` |
| takes a held turn back: a silent holder, the operator's stop, a restart or shutdown; answers a claim it cannot record | the role whose turn it was (`worker`, `reviewer`): the turn is that role's, its instance is the one that stops, and the map reads worker → coordinator (`task.released`) when dispatch offers it again; `instance` in the data when addressed to one | `task.released` |
| says at each start what makes dispatch act | `coordinator/dispatch` {mode: continuous, kind: program, listens_for: [gate.conflict, gate.rejected, task.released, ticket.created], log} | `orchestra.machine.started` |
| records a step another machine announced (a claim, a heartbeat, `review.requested`, `changes.requested`, `review.approved`, `task.reported`) | nothing: recorded in the store only | none |
| runs the gate | `merge_gate` (a machine: it does work of its own, merging) | `gate.started` (`causationid` = the approval), `gate.landed`, `gate.rejected`, `gate.conflict`, `gate.error`, `gate.base_broken`; in code mode its announcement {package: tasks, mode: continuous, kind: program, listens_for: [review.approved], log} |
| external review: hands an approved branch off for its pull request | `merge_gate`; `causationid` = the approval | `gate.handed_off` |
| external review: sends the work back to the worker that did it after feedback on its pull request | whoever gave the first of the feedback, as the bridge named them (`person.<login>`, a bot); `causationid` = that bridge event. Not taken within the return timeout: withdrawn and offered to any worker by `coordinator/dispatch` (the rows above) | `changes.requested` (to that worker) |
| external review: asks a planner about a ticket sent back by its pull request at a multiple of the threshold | `merge_gate` | `task.attention_needed` |
| external review: asks a planner about a pull request closed without a merge | `merge_gate` (whose hand-off it was) | `task.attention_needed` |
| pauses the pipeline | `merge_gate` | `pipeline.paused` |
| deploys or restarts staging | `merge_gate` | `staging.deployed`, `staging.failed`, `staging.restarted` |
| asks a planner about a ticket blocked by the worker's report, a worker turn that keeps failing, nothing beyond the base | `worker` (whose work is stuck) | `task.attention_needed` |
| asks a planner about a ticket rejected at a multiple of the threshold by reviews (or by reviews that kept ending without a verdict) | `reviewer` | `task.attention_needed` |
| asks a planner about gate rejections at the threshold, the base broken, a gate error, a landed `orchestra.toml` the root did not follow, the pipeline still paused after a restart, staging failed | `merge_gate` | `task.attention_needed` |
| makes a recurring ticket at its due time (`[[tasks.recurring]]`, section 7) | `planner/recurring`, the planner's scheduled engagement: deciding what the work is, is a planner's; `correlationid` = the occurrence (`<name>@<local due time>`), no `causationid` (nothing caused it but the clock); the map reads planner → coordinator (`ticket.created`) | `ticket.created` {ticket, title, by, recurring, occurrence, late}; at the first tick with a definition, `orchestra.machine.started` {mode: scheduled, kind: program, listens_for: [], recurring: [names]} |
| asks about a recurring ticket it could not make (no open milestone) | `planner/recurring` | `task.attention_needed` (to `supervisor`) |
| runs the `[[probes]]` | `probes` (the condition checks; whether they should become instruments is an open question: Orchestra's `docs/roadmap/story-placement.md` §7 item 2) | `probe.failed`, `probe.recovered` |
| the operator's controls (web) | `task_web` | `capacity.changed`, `pickup.paused`/`resumed`, `kill.requested` |

`tasking staging deploy` from the command line publishes as `task_cli`.

## 4. The scheduler (`tasking/scheduler.py`, `tasking run`)

`orchestra run` starts it: the plugin declares it in `Plugin.processes` (`tasking/web.py`), so
Orchestra's launcher keeps `tasking run --wait` running as `tasks.scheduler` while the project's
`orchestra.toml` has a `[project]` or `[tasks]` section, and restarts it when it exits. One process
per project: a second `tasking run` for the same project exits, naming the pid that runs it; with
`--wait` (as `orchestra run` starts it) it waits for that one to stop and then takes over. On start it recovers (above). Every tick (`[tasks] tick`, 3 s by default) it:

1. reloads `orchestra.toml`, reaps finished threads, and for any ticket the operator asked to
   stop releases the assignment and sends the ticket back at once, counting it against no one;
2. **follows the bus** and records what the machines did, in the bus's order: the first valid
   claim of an offer names the holder, moves the ticket to the role's active state and starts
   the lease (a later claim lost, and is told nothing); a heartbeat extends the lease; the event
   that ends a turn is applied to the ticket and the turn's assignment dropped, and when it offers
   the next turn (a review requested, changes requested) that offer is recorded in its place, with
   the same workspace, as the machine made it (an end that cannot be applied still frees the
   ticket: back to waiting, with a note); then every lease that ran out is released: a holder that
   went quiet is a crash, an offer nobody took is dropped and rests before the queue offers it
   again (§3: a pause that doubles, cut short when the ticket changes or a machine that listens for
   `task.assigned` starts), and rejected work its worker did not take back within the return
   timeout goes to any worker at once;
3. **offers waiting work** from the queue: reviews first (finished work lands before new work
   starts), then, unless pickup is paused, ready `OPEN` and `REJECTED` tickets in
   effective-priority order, up to the role's capacity (every recorded offer and turn counts,
   the ones machines offered each other included; an offer not yet claimed holds a slot for its
   lease). A `REJECTED` ticket goes first to the worker that did it (`to`, for the return
   timeout), unless it already waited for it since it was rejected. Capacity is the operator's ceiling from the Dashboard if set, otherwise
   `[tasks] workers`/`reviewers`, with a planner's dial (`set_capacity`) under it;
4. in code mode, with external review, hands every `APPROVED` ticket off for its pull request
   (section 5) after applying what the pull-request bridge published since the last tick;
   otherwise starts a gate run for the oldest `APPROVED` ticket if none runs and the
   pipeline is not paused (with reused worktrees, section 5, this comes before step 3, so the
   gate's claim on the pool comes before any offer's), and drops the worktrees of cancelled
   tickets;
5. between steps 2 and 3, makes the **recurring tickets** that are due (section 7, "Recurring
   tickets"), so one is offered in the tick it is made;
6. keeps staging running (section 5) and runs the due **probes** (`tasking/probes.py`:
   `[[probes]]` commands on an interval; `probe.failed`/`probe.recovered`, as `probes`, fire on
   the transition).

It writes no metrics: the record keeps the counters, and an instrument reads them (section 8).

What the end of a turn makes of a ticket is `tasking/reports.py`. A `task.reported` is usable
only if it is a worker's `BLOCKED` with a `blocked_reason`, or no report; anything else (a
`READY_FOR_REVIEW` or a verdict sent as a report included) counts as no report, with what was
wrong as the machine's last words.

**Recording a review request.** The worker's summary becomes a note and the ticket
`READY_FOR_REVIEW`; in code mode the commit it names must be on the ticket's branch (the record
reads the bus behind the machines, so the branch may have moved on since: the commit need not be
its head) and becomes `reviewed_commit`, and the worker's test output is kept for a review the
queue may have to offer later. A commit with nothing beyond the base blocks the ticket; a request
that names no commit, or one not on the branch, is no report. The review it offers is recorded as
offered by the worker; one the record cannot stand behind (another ticket, role or workspace, no
id) is not, and the ticket waits for the queue's offer instead.

**Applying a worker's report.** `BLOCKED` records the reason and asks the coordinator. No report
(the lease ran out, or the report was unusable) is a crash: up to two in a row go back to `OPEN` with a note and are offered
again; the third blocks the ticket. `failed` true in the report means the machine went away or
its lease ran out, retried up to twice; `failed` false with a null report means it ended on
purpose without one, and the ticket is `BLOCKED` for a planner. An operator stop, a restart or
a shutdown goes back to `OPEN` without counting; an offer nobody accepted changes nothing. Notes
and feedback written by the task system are signed `task_scheduler` and state what happened:
that name is on the record's notes, never an event's source. Each move is counted `by` the
machine whose act or responsibility it was (section 8): a crash or a block of a worker turn is the
worker's.

**Recording a verdict.** `review.approved` moves to `APPROVED` (and in tasks mode straight to
`DONE`); `changes.requested` records the feedback and sends the ticket back, `rejected_by` the
reviewer, and records the offer it makes to the worker (`to`) with the return timeout as its lease.
A verdict naming another commit than `reviewed_commit` is no verdict. No verdict (a lease that ran
out included) is not a rejection: the review is redone, up to three times, before the ticket is
sent back by the task system (`rejected_by` `task_scheduler`, for the worker to read; counted,
and any attention published, as `reviewer`, whose responsibility the review is); the merge gate's
send-backs say `merge_gate`. A ticket whose folder or worktree (with reused worktrees, its branch) is gone when
the queue would offer its review is rejected by the task system without a review round, since
nothing was reviewed. On shutdown the scheduler lets a gate run
finish before it releases anything.

**Requests** are served by the desk, a thread in the same process (section 3). It reads on from
where the last desk stopped (the bus offset and the request ids answered from there, in the
runtime record), so a request made while no scheduler ran is answered when one starts: applied
if it is younger than twice what `ask` waits (`RESUME_S`, 60 s), refused with the reason if
older, since its asker gave up long ago and a change applied late would surprise everyone. A
restart answers no request twice. A machine's claim, report or hand-off for an assignment the
restart dropped is ignored, with a line in the log (a claim is answered with `task.released`): the
ticket was offered afresh. The scheduler's log is `<state>/logs/scheduler.log`. It announces
nothing of itself, so the Machines page has no machine for it; in code mode the gate's
announcement names this log as `merge_gate`'s. The process's own output is Orchestra's
`logs/machines/tasks.scheduler.log`, shown on the Core page.

## 5. Work modes: tasks and code

`[project] work` chooses what a ticket's workspace is.

**`tasks`** (the default): the workspace is a folder, `<state>/workspaces/<ticket>/`, made when
the ticket is first offered and kept for the reviewer and for any further turn. It is empty at
first and holds nothing of the repository: a task about the repository says so in its own words
(the demo's worker runs its commands there with `ORCHESTRA_REPO` naming the repository; the
README's section on writing tickets has the rule). Nothing is merged; approval is done. The
result of a task is whatever the worker left there (the demo's worker writes `RESULT.md`) and
its report.

**`code`**: the workspace is a git worktree at `<state>/worktrees/<ticket>/` on a branch
`orchestra/<ticket>-<slug>` from `base_branch`, reused by every turn, removed when the ticket is
done or cancelled. Every code offer names the project's first `[[gate.unit]]` command
(`unit_command`) and the base branch (`base`). The worker runs the command on the commit it asks to
have reviewed and puts the output and the commit in its request (`review.test_output`,
`review.commit`), so the reviewer sees the facts; the gate runs every spec again before landing.

**Reused worktrees** (`[tasks] reuse_worktrees = true`, code mode; `tasking/pool.py`). For a
repository whose checkout is large, the task system keeps a pool of at most `worktree_pool`
worktrees at `<state>/pool/<n>/` instead of one per ticket, and resets one for each turn it offers
from the queue (a worker's, a reviewer's) and for each gate run. A turn one machine offers another
directly (a review requested, changes requested) is handed the worktree the work is in: the
record passes the hold with it, and the machine that passes it on leaves it at the commit it
names (section 3). Off (the default), nothing here applies.

- *Reset*: an unfinished merge or rebase is aborted, tracked changes are discarded (`git reset
  --hard`), untracked files are removed (`git clean -ffd`), and the ticket's branch is checked
  out: created from `base_branch` when new, the ticket's own branch with its commits when it
  comes back. Ignored files (build caches, `node_modules`: the reason to reuse) are kept, or
  removed too with `worktree_clean_ignored = true` (`-x`). A worktree that is not clean after
  this is an error, and the turn is not offered.
- *Free*: a worktree is free when no recorded assignment holds it and no gate run does. The store's
  record of the pool (`runtime.json`) names each worktree's holder, an assignment's id or the
  gate's; an assignment that is dropped, for whatever reason (a report, a lease run out, a
  restart, the operator), frees its worktree with it, so a crash never leaves one held. A
  worktree a crash left dirty is reset like any other before its next turn.
- *The work is on the branch, not in the worktree*: while it waits in the queue a ticket holds no
  worktree. A worker commits its work before it asks for a review. A turn that ends without a
  report loses what its worker did not commit; the next worker's feedback says so.
- *Rework*: a ticket's next turn (a review, a rejection back to its worker, a crash retry) gets
  the worktree it last used if that one is free, so its caches are warm; otherwise the one
  claimed longest ago. A preference, never a wait.
- *The gate uses the pool too*: a gate worktree is as large as a worker's, and the pool's size is
  the number of checkouts on disk, which is the point. The scheduler claims the gate's worktree
  before it makes any offer in a tick, so landing comes before new work; the base check after a
  failure runs in the same worktree, reset to the base. The gate's temporary branches are deleted
  as before; a worktree left on one by a crash is detached when the next scheduler starts.
- *Pool full*: the turn is not offered; it waits for a worktree, never a new one beyond the
  size. The scheduler's log says so once per ticket (`<ticket>: the worktree pool is full (N in
  use); the worker waits for a free one`) and `tasks_worktrees{state="waiting"}` counts the turns
  waiting (section 8). It is back-pressure like capacity, not a question for a planner, so no
  attention is asked. A pool smaller than workers plus reviewers plus one makes turns wait; the
  default is that sum.
- `tasking doctor` reports pool directories the record does not know (`--fix` removes them) and a
  pool larger than its size (after `worktree_pool` was lowered; the extra worktrees stay until
  removed by hand). Turning the option off leaves the pool's worktrees on disk; turning it on
  while tickets have their own worktrees leaves those until the tickets land.

**The merge gate** (`tasking/gate.py`) lands approved code, one ticket per run: a gate
worktree on a temporary branch from the base head, `git merge --no-ff` of the reviewed commit (a
conflict sends the ticket back to its worker without counting as a review round; one kind of
conflict the gate resolves itself, in `tasking/appends.py`: when `orchestra.toml` is the only
conflicted file and both sides only added whole tables at the same place, such as two tickets
that each appended a `[[collect]]` entry, it keeps both, the base's first, if the result parses,
and says so in the run's summary), then the
stages from `orchestra.toml`: `[build]` (the artifact is copied to `<state>/artifacts/<run>/`),
every `[[gate.unit]]` spec, and if `[acceptance]` and `[[gate.acceptance]]` are set, the artifact
started on a free port and the acceptance specs run against it. All pass: the base branch is
fast-forwarded (pushed if `push = true`), the ticket is `DONE`, its worktree and branch removed,
and the build deployed to staging. The repository root's files follow only when it has the base
branch checked out, clean: then it is fast-forwarded there (an untracked file at a path the landing
adds stops it with a gate error naming the file). With another branch checked out, only the base's
ref moves; when the landing changed `orchestra.toml`, which Orchestra's launcher reads from the
root, the run's summary says so and the coordinator role is told that the change takes effect only
when the root is updated. A failure: the base is checked alone; if the base itself fails
the pipeline pauses and the supervisor role is told; otherwise the ticket is rejected with the
failing stage's output as feedback.

**External review** (`[gate] mode = "external"`, code mode; `tasking/external.py`; goal G6 in
Orchestra's `docs/roadmap/goals.md`). Where code reaches the base branch through a company's
pull requests and CI/CD, the gate lands nothing. It hands the approved branch off for a pull
request, and the ticket follows that pull request until it is merged or closed.

- *Hand-off.* The gate (`merge_gate`) moves an `APPROVED` ticket to `IN_EXTERNAL_REVIEW`, keeps the
  reviewed commit as `pull_head` (the commit the pull request is to carry), and publishes
  `gate.handed_off` {ticket, branch, commit, base, repository, pull} with the approval as its
  cause. It runs no stages and merges nothing: the company's checks run on the pull request.
  **Pushing the branch and opening the pull request are the owner's acts** (or, one day, an
  outbox's with the owner's approval): the task system never pushes and never opens one. The
  ticket's worktree is kept for the rework that may come.
- *Link.* What the pull-request bridge (orchestra-bridges, `github.pr.*`) publishes is linked to a
  ticket by the pull request's head branch, which is the ticket's branch (`orchestra/<ticket>-<slug>`:
  the owner pushes it under that name), and from then on by the pull request itself (`pull`,
  `owner/repo#n`). A ticket follows one pull request at a time; a new one opened from its branch
  replaces it. Only `[gate.external] repository`'s pull requests are read when it is set.
- *Feedback re-engages the worker.* While the ticket is `IN_EXTERNAL_REVIEW`, a review requesting
  changes, line comments (`line_comments`, on by default; a commenting review with line comments
  counts too), conversation comments (`conversation_comments`, off), a check that ends `failure`,
  `timed_out` or `error` (`failing_checks`, on) or a label in `failing_labels` (`SAIL-failed`) is
  feedback. It is gathered until none came for `settle` (15 s by default; a review's line comments
  arrive just after it, so a review waits for as many as it says it has, up to three settle
  periods), then the work goes back once: `REJECTED` (a review round, so the coordinator is asked
  at the threshold as for any rejection, by the merge gate rather than in a person's name; `pull_rejections` + 1; `rejected_by` the machine that gave
  the feedback) and `changes.requested` addressed `to` the worker instance that did the work, with
  the return timeout, like a reviewer's (section 3). The worker resumes its own work (its session,
  if it keeps one) on the same branch, asks for a review as before, and an approval hands the new
  commit off again; the owner pushes it to the pull request. **The bus carries no text**, so the
  feedback is metadata only: who, the review's state, how many line comments and at which files
  and lines, which label, which check and how it ended. The feedback tells the worker that the
  texts are on the pull request (`brief.pull` names it).
- *Which machine re-engages.* The `changes.requested` is published as whoever gave the first of the
  feedback, as the bridge named them (`person.alice` for a review, the pipeline's account for a
  label, `bot.<app>` for a check), with the bridge's event as its cause. Asking the worker for
  changes is a reviewer's act (section 3: reviewer → worker); here the reviewer is a person or a
  bot on the pull request who speaks no protocol, and the scheduler stands in for that side of it,
  as it stands in for the coordinator's dispatch (D17: it publishes as the machine whose
  responsibility it implements). So Orchestra's hand-offs read merge_gate → the pull request's
  reviewer (`gate.handed_off`) → worker (`changes.requested`). If that worker does not take it back
  within the return timeout, the re-engagement falls back to the queue like any rejected work:
  dispatch withdraws the offer and offers it to any worker, both as `coordinator/dispatch`, and the
  hand-offs read worker → coordinator → worker (`task.assigned`). The pull request's reviewer offers
  only the turn back to the worker that did it; it never offers from the queue. Rejected: a configured stand-in machine for
  "the pull request's reviewers", which would be a machine that does no work (Orchestra's D18);
  the bridge itself, which only reads GitHub and decides nothing; the merge gate, which did not
  ask for the changes.
- *Stale feedback.* Feedback about another commit than `pull_head` (the bridge's `head`: the commit
  reviewed, labelled or checked) is noted on the ticket and not acted on, and so is feedback that
  comes while the ticket is not in external review (the worker already has it back). `pull_head`
  moves with every hand-off and with a push the bridge reports (`github.pr.pushed`) that is not
  older than it.
- *Only a merge closes it.* An approval and a label in `passing_labels` are noted. `github.pr.merged`
  makes the ticket `DONE` from whatever waiting state it is in (whoever holds it or is offered it
  is released first, without counting against anyone), and its worktree and branch are removed as a
  landing removes them. `github.pr.closed` (never a merge's own close) makes a ticket in external
  review `BLOCKED` with the reason, and the merge gate, whose hand-off it was, asks the coordinator:
  a closed pull request is a decision someone made, so redoing the same work automatically would
  only open the same pull request again; a planner reopens it, unblocks the ticket (to have the work
  redone for a new pull request) or cancels it. `github.pr.reopened` returns it to
  `IN_EXTERNAL_REVIEW`.
- *Why a state of its own.* A ticket in external review waits on people outside, for hours or
  days, and is neither approved-and-waiting-for-the-gate (`APPROVED`, which the gate would take)
  nor held by any machine (an active state needs a lease: no zombies). `IN_EXTERNAL_REVIEW` is a
  waiting state, so the measurements show it as a stock of its own (`tasks_tickets{state}`), and
  every rule about waiting states (cancelling, the restart's recovery) holds for it unchanged.
- *Restarts.* External review reads the bus on from where it stopped (the offset, the ids it
  applied lately and the feedback it is gathering, in `runtime.json`), so a merge published while
  no scheduler ran is applied when one starts, and nothing is applied twice.

**Staging** (`tasking/staging.py`): when `[staging] start` is set, one long-lived instance of the
last build that passed the gate, started with the artifact and port as placeholders, probed at
`health`, restarted by the scheduler if its process dies or its health check goes unanswered
for 10 minutes, optionally behind a named cloudflared tunnel (`[staging.tunnel]`). A restart
that fails is tried again with a pause that doubles (30 s up to 30 min); each failure is
`staging.failed`, and the first tells the supervisor role. A failed deploy rolls back to the
previous build and tells the supervisor role. `tasking staging status|logs|deploy|restart|stop` operates it.

## 6. Writing a machine that fills a role

A machine is a program that reads Orchestra's bus, takes offers for its role, does the work, and
passes it on. It needs Orchestra as a library and nothing from this package; the contract is the
events of section 3. The demo's `demo/machines/shell_worker.py` and
`demo/machines/check_reviewer.py` are each a few pages and are the reference (a copy of the shell
worker lives in projects made from it, such as orchestra-sample-live; a copy speaks the contract it
was copied with). The changes of 2026-10-27 (the scheduler is no machine; offers published as
`coordinator/dispatch`, releases as it or the role) changed neither program and need no change in a copy:
nothing in them reads an offer's or a release's source, and the claim rule (`holder`) is the
bus's order. The shape:

1. **Open the bus as yourself.** `Events.for_project(load_project(), source="worker/1")`
   (`orchestra.config`, `orchestra.events`): the source is `<machine>/<instance>`, so Orchestra
   sees one machine `worker` with an instance `1`, and the record knows who holds what.
   `load_project()` takes the repository from `ORCHESTRA_REPO`, else the current directory: run
   the machine from the repository, or set `ORCHESTRA_REPO` to it, as `demo/run.py` does.
2. **Announce yourself**: publish `orchestra.machine.started` with `mode` (`continuous`), `kind`
   (`program`), `listens_for` (a worker: `["task.assigned", "changes.requested",
   "task.released"]`; a reviewer: `["task.assigned", "review.requested", "task.released"]`) and,
   if you keep one, `log`. The Machines page shows the machine from this and from what it then
   publishes, and names the event a hand-off came by from what the receiver listens for.
3. **Follow the bus from now.** `offset = events.end()`, then in a loop read what is new
   (`evs, offset = events.after(offset)`), keeping the offset each read started from. An offer
   for you is an event of section 3's three offer types whose `data.role` is yours and whose
   `to`, if any, is your source: the ticket's `brief` (title, description, acceptance, feedback
   from a rejection, …), the `workspace` to work in, in code mode the `branch`, `base` and
   `unit_command`, and a reviewer's `review`.
4. **Claim**, only what you can do within the lease (`lease_s`): publish `task.accepted`
   {assignment, ticket, role, instance} with `subject=ticket` and the offer's id as
   `causationid`. Then read the bus from where you read the offer until your claim is there:
   you hold the turn if no valid claim of the same offer came before yours (section 3). If one
   did, you lost: do nothing more, say nothing more. A machine that is the only kind on its role
   may take work it cannot do and report `BLOCKED`, so a planner hears of it (the demo's worker
   does this for a ticket without commands); where several kinds of machine serve one role,
   each claims only what it can do. An offer nobody claims is visible only as `task.released` on
   the bus, and is offered again after a pause, or at once when a machine announces that it
   listens for `task.assigned`.
5. **Work, and say so**: do the task in the workspace; publish `task.working` {assignment} well
   within the lease (`HEARTBEAT_S`, 30 s; the demo's worker says it every sixth of the lease), so
   the lease goes on (a review that takes a moment needs none). If `task.released` names your
   assignment (and your instance, or no instance), stop what you are doing, the heartbeat and the
   end of the turn with it: the ticket is no longer yours.
6. **Pass the work on.** A worker that has finished asks the reviewers: `review.requested` with
   a new `assignment` (`<ticket>.reviewer.<8 hex>`), `role` `reviewer`, `ends` (your assignment),
   the offer's `workspace`, `branch`, `base`, `lease_s`, `unit_command` and `brief`, and `review`
   {summary, commit, test_output, worker: your source}; in code mode commit first, name the
   commit, and run `unit_command` on it for `test_output`. A reviewer that rejects asks the worker
   for changes: `changes.requested` with a new `assignment` (`<ticket>.worker.<8 hex>`), `role`
   `worker`, `to` (the offer's `review.worker`), `ends`, the same workspace fields, the brief with
   `feedback` and `rejected_by` set, and `summary`, `feedback`, `commit`; it leaves the workspace
   at that commit (nothing untracked). A reviewer that approves publishes `review.approved`
   {ends, ticket, role, summary, commit}. Work that passes to no machine ends with `task.reported`
   {assignment, ticket, role, report, failed: false, text}: a worker's `{state: BLOCKED, summary,
   blocked_reason}`. The summary becomes a note on the ticket; the feedback is what the worker is
   told; the blocked reason is what the planner reads in the banner.
7. **Take your rejected work back.** A worker keeps listening for `changes.requested` addressed
   to it after it asked for a review: it is the same work, best resumed by the instance (and the
   session, if it keeps one) that did it. If it does not claim it within the return timeout, the
   work goes to any worker.

A worker that cannot act on a rejection's feedback must report `BLOCKED` instead of redoing
identical work; otherwise a program worker and a program reviewer argue forever, with an
attention event at every multiple of the threshold and nothing else stopping them: rejected work
goes back to its worker at once and the task system has no floor of its own without a planner. The demo's
worker does this when a rejected ticket would rerun the same commands. A reviewer that cannot
judge a criterion says so in its summary. The scheduler never needs to know why; it moves the
ticket and asks the planner.

A planner is a machine too: it publishes requests (section 3) and listens for
`task.attention_needed`. `tasking api --as <role> <op> --args '<json>'` is a planner's request
from the shell; `demo/run.py` makes the demo's milestone and tickets that way.

## 7. Configuration (`tasking/config.py`)

The task system's settings are sections of the repository's `orchestra.toml` that Orchestra
itself ignores. `tasking init` writes starters for them after Orchestra's own.

```toml
[project]
base_branch = "main"
ticket_prefix = "ORC"
work = "tasks"           # tasks: a folder per ticket, nothing merged | code: a branch per ticket, the merge gate
push = false             # code: push the base branch after a landing

[tasks]
workers = 2              # tickets offered to workers at once
reviewers = 1
return_timeout = "60s"   # rejected work waits this long for the worker that did it, then goes to any worker
review_rounds_before_escalation = 3
aging_days_per_level = 2 # a ticket gains one priority level per N days of age; 0 disables
reuse_worktrees = false  # code: keep a pool of worktrees and reset one per turn (section 5)
worktree_pool = 4        # the pool's size, the gate's included (default workers + reviewers + 1)
worktree_clean_ignored = false   # a reset also removes ignored files (build caches)

[build]                  # code, optional: the artifact the gate builds
command = "uv build"
artifact = "dist/*.whl"
timeout = "600s"

[acceptance]             # code, optional: how the gate runs the artifact for the acceptance specs
start = "{python} -m myapp serve --port {port}"   # {python}: a venv with {artifact} installed
ready = "http://127.0.0.1:{port}/health"
ready_timeout = "60s"

[gate]
timeout = "900s"         # per spec
mode = "land"            # land: the gate merges and lands | external: it hands the branch off for a pull request (code mode)

[gate.external]          # with mode = "external" (section 5, "External review")
repository = "acme/service"        # only this repository's pull requests (any, when unset)
failing_labels = ["SAIL-failed"]   # a label added that sends the work back to its worker
passing_labels = ["SAIL-passed"]   # noted; only a merge closes the ticket
line_comments = true               # review comments on lines re-engage the worker
conversation_comments = false      # comments on the conversation re-engage it
failing_checks = true              # a check ending failure, timed_out or error re-engages it
settle = "15s"                     # gather feedback until none came for this long, then re-engage once

[[gate.unit]]
name = "unit"
command = "uv run pytest -q tests/unit"

[[gate.acceptance]]
name = "acceptance"
command = "uv run pytest tests/acceptance -q --base-url {url}"

[staging]                # optional
start = "{python} -m myapp serve --port {port}"
port = 8001
health = "/"
health_timeout = "60s"
stop = ""
[staging.tunnel]
name = "myapp-staging"
hostname = "staging.example.com"

[[probes]]               # optional: conditions checked on an interval; exit 0 = holds
name = "staging_fast"
command = "scripts/p95_under_1s.sh $STAGING_URL"
every = "5m"             # Orchestra's durations: ms, s, m, h (a day is "24h")
timeout = "60s"
```

### Recurring tickets (`tasking/recurring.py`, `tasking/schedule.py`)

Datetime scheduling is the task system's (Orchestra's decision D7): a scheduled piece of work is
a ticket made from a template at each due time of a schedule. Each `[[tasks.recurring]]` table is
one definition. It is read through Orchestra like every setting (so the owner's private overlay
may hold some), re-read every tick, and checked by `tasking recurring check` (exit 1 and a reason
per problem), by the query `check_recurring` on the bus (`tasking api check_recurring`; with
`{"definitions": [...]}` it checks a draft instead, before it is written: the answer has `valid`,
each valid definition with its next due time, and `problems`, the reasons per definition), or by
`tasking.recurring.check(project.config.raw, store)` from a program: a
definition with any problem is not used, and the others are; the scheduler says each problem
once in its log. A program that sets them up on request (the Supervisor) changes them through
Orchestra's `config_edit` and runs the check after. An occurrence is made at the first tick at
or after its due time (`[tasks] tick`, 3 s by default).

```toml
[[tasks.recurring]]
name = "morning-brief"        # required: [a-z0-9][a-z0-9_.-]*, at most 64, unique; the occurrence ids and the series label use it
every = "day"                 # "day" | "week" | "month" (or daily, weekly, monthly) ...
at = "07:30"                  #   ... at this 24-hour local time (required with every)
on = ["mon", "tue", "wed", "thu", "fri"]   # day: optional weekdays; week: required weekday(s);
                              #   month: required day(s) 1-31 or "last" (29-31 fall on a shorter month's last day)
# cron = "30 7 * * mon-fri"   # ... or five crontab fields instead of every/at/on (minute hour day-of-month month
                              #   day-of-week; *, a-b, */n, lists, jan..dec, sun..sat, 0 and 7 Sunday;
                              #   both day fields restricted: either matches, as cron)
timezone = "Europe/London"    # required: an IANA zone; the schedule is that zone's wall clock
title = "Morning brief for {weekday} {date}"   # required
description = "Write the brief for {date}."    # optional, as are acceptance, objective, verification
acceptance = "- the brief is in briefs/{date}.md"
milestone = "M1"              # required: an open milestone's id, or its name when only one has it
priority = 2                  # 1 (soonest) to 9; default 5
if_open = "skip"              # skip (default): make nothing while the previous occurrence's ticket is not
                              #   DONE or CANCELLED, with a note on it | create: make one anyway
catch_up = true               # default true: after a gap, make the latest missed occurrence (late);
                              #   false: an occurrence later than late_after is missed, not made
late_after = "10m"            # default 10m: made later than this after its due time, it is late
enabled = true                # default true; false stops it (back on, it counts from then)
```

The placeholders in the texts are the occurrence's local due time: `{date}` 2026-10-27, `{time}`
07:30, `{weekday}` Tuesday, `{day}` 27, `{month}` 10, `{month_name}` October, `{year}` 2026,
`{week}` 2026-W44 (ISO), `{quarter}` Q4, `{due}` 2026-10-27T07:30+00:00; `{{` and `}}` for braces.
Any other key is refused with the reason: `role`, `worker_kind` and `kind` because every ticket
goes to the worker role (the task system has no worker kinds yet, goal G3), `depends_on` because
an occurrence waits for the previous one only through `if_open`.

**The rules.**

- **A due time** is a wall-clock time in the zone, so it follows daylight saving. A time the
  clocks skip (02:30 when they spring forward) is due when they reach it (03:30 on the wall); a
  time that happens twice (01:30 when they fall back) is due once, the first time. Cron's way.
- **Once.** The record keeps per definition the due time of the last occurrence handled
  (`tickets.json`, `recurring`), and the store checks it in the same locked write that makes the
  ticket. A restart, a second tick or a second writer never makes an occurrence twice; the
  occurrence's id is `<name>@<local due time, YYYY-MM-DDTHH:MM>`, on the ticket (`occurrence`)
  and as the `correlationid` of its `ticket.created`.
- **Catch-up.** When the scheduler was not running across due times (the laptop slept), the
  next tick makes **at most one**: the latest due one, marked late (`late: true`) when it is made
  more than `late_after` after its due time. The ones before it are counted as missed, never
  made. With `catch_up = false` a late one is missed too.
- **If the last one is open** (`if_open = "skip"`): the occurrence is skipped (counted, and noted
  on the open ticket), and the next due time is tried as usual.
- **New, changed, back.** A definition seen for the first time, or back after it was removed,
  disabled or invalid, counts from that tick: nothing before it is owed. A changed schedule counts
  from the last occurrence handled.
- **Cannot be made** (the milestone is gone or closed): the occurrence is counted as failed, and
  the Supervisor is asked once (`task.attention_needed` to `supervisor`, as `planner/recurring`);
  it is not tried again every tick.

From there the ticket is like any other: dispatch offers it, a worker does it, a reviewer judges
it, the gate lands it in code mode, and a worker that cannot do it blocks it for a planner. The
next due times are on the Tasks panel and at `GET /api/plugins/tasks/recurring` (section 9), and
in `tasking recurring list`; what became of each due time is counted (section 8).

`[events]` and `[metrics]` are Orchestra's: the task system opens the bus through Orchestra's own
project (`tasking.config.Project.orchestra`), and its measurements reach the metrics store through
the instrument `orchestra run` runs (section 8), so where they go is decided in one place. Capacity and the config are re-read every tick, so a change is live.

## 8. State

```
~/.orchestrator/<key>/                 # Orchestra's; <key> is <repo name>-<hash of its path>
  events.jsonl                         # the file bus (when [events] url is "file")
  tasks/                               # the task system's
    tickets.json                       # tickets, milestones, notes, counters, the metric counters,
                                       # per recurring definition the last due time handled
    runtime.json                       # scheduler pid, assignments, capacity, pause, kill requests,
                                       # worktrees, the worktree pool, gate runs, builds, pipeline, probes, staging,
                                       # where the desk reads requests from next, the turns waiting for a pool worktree
    .lock                              # one file lock around every read-modify-write
    workspaces/<ticket>/               # tasks mode
    worktrees/<ticket>/                # code mode
    pool/<n>/                          # code mode with reused worktrees
    gate/<run>/  artifacts/<run>/  staging/
    logs/scheduler.log  logs/<run>/<stage>-<spec>.log  logs/review/<ticket>/
```

Only `tasking/store/` reads or writes these JSON files (`store/tickets.py` the first,
`store/runtime.py` the second, behind one `LocalStore`).

**Metrics** (`tasking/measures.py`) come from an instrument, not from the scheduler (Orchestra's
rule 7: the metrics store is filled by instruments). The record keeps cumulative counters
(`metric_counts` in `tickets.json`), moved in the same locked write as the change they count, so
a level and the counters read from one record balance exactly. The plugin declares the instrument
(`Plugin.collectors`: `Collector.collect("tickets", "<python> -m tasking metrics", every_s=<[discovery]
step>)`), so `orchestra run` runs `tasking metrics` every discovery step and labels every point
`instrument=tasks.tickets` (and `project`). The same Prometheus text is served at
`/api/plugins/tasks/metrics`. It is a command, not a scrape of that URL, because `orchestra run`
does not know the web server's port, and a port of the scheduler's own would be one more thing to
configure per project; the text is the same. The series:

- `tasks_tickets{state}`: tickets in each state, every state, read every step (`IN_EXTERNAL_REVIEW`
  included, 0 without external review);
- `tasks_tickets_created_total{by}`: tickets created, `by` the machine that asked;
- `tasks_ticket_transitions_total{from,to,by}`: state changes;
- `tasks_recurring_occurrences_total{recurring,outcome}`: each recurring ticket's due times and
  what became of them: `created` (on time), `late` (made more than `late_after` after its time),
  `skipped` (the previous one still open), `missed` (not made: a later one was, after a gap, or
  `catch_up = false`), `failed` (no open milestone). A recurring ticket made is also counted in
  `tasks_tickets_created_total{by="planner"}`;
- with reused worktrees (section 5), `tasks_worktrees{state}`: the pool's worktrees `in_use` and
  `free`, and the turns `waiting` for one (kept in `runtime.json`).

`by` is the machine whose act or responsibility moved the ticket, never `task_scheduler`: a claim,
the holder's machine; `review.requested`, `worker`; a verdict, and approval to `DONE` in tasks
mode, `reviewer`; the gate's moves (to `MERGING`, `DONE`, `REJECTED`, back to `APPROVED`),
`merge_gate`; a lease that lapsed, a crash sent back to the queue, or a worker turn blocked,
`worker`; a review redone for no verdict, or sent back by the task system's rules (no verdict, a
missing workspace), `reviewer`; a restart sending an active ticket back, the machine whose turn it
was (`IN_PROGRESS` worker, `IN_REVIEW` reviewer, `MERGING` merge_gate); a request's move (cancel,
unblock) and a creation, the asker; with external review, the hand-off `merge_gate`, and a move a
pull request's event caused (feedback, merge, close, reopen) the machine the bridge named for it
(`person.<login>`, a bot).

Until 2026-10-27 the scheduler wrote these points itself, with `machine="task_scheduler"` on every
series, and re-asserted the levels every discovery step between changes. That label is gone and
`instrument="tasks.tickets"` is new; the series' names are kept, so a store's history splits at
the upgrade by label, not by name. Counters kept before then carry `machine` in their key and are
summed without it. Orchestra's `docs/roadmap/story-placement.md` §7 item 3 lists this package
among those whose machines wrote their own metrics; it no longer does.

## 9. The plugin: pages and API (`tasking/web.py`, `tasking/static/`)

Inside Orchestra's web UI, under the plugin name `tasks`:

- **Tickets** (`/tickets`): the graph of milestones and tickets (dagre), done tickets collapsed
  into "via n done" edges, a drawer per ticket (what needs attention first: the blocker, the
  rejection, why it waits; then its fields, texts and notes). The gate's result is on the ticket
  as the `merge_gate` note and feedback.
- **The Tasks panel** on the Dashboard (`tasks-panel.js`): the tickets in flight with the
  instance that holds each and since when, or to whom it is offered, the queue by state, the
  capacity and pause controls, and the recurring tickets' next due times (read-only; how many
  definitions are not used, when any is not).
- **The blocked-ticket banner** on every page (`attention.js`): which tickets wait for a
  decision; a package that shows them itself calls `TaskAttention.standAside()`.

`tasks-boot.js` is the one script Orchestra loads on every page: it loads `tasks.js` (the shared
code and the controls) and then the two scripts above (`tasks-panel.js`, `attention.js`). The look every page needs, `tasks.css`, is a
stylesheet the plugin declares (`stylesheets`), which Orchestra links in every page's head. The
Tickets page adds its own `tickets.js` (the graph) and `tickets.css`. The plugin declares no
machine: the scheduler is a process (`Plugin.processes`), shown on the Core page, and its
machine card is gone; the Tasks panel keeps the queue and the controls. It declares one
instrument, `tasks.tickets` (`Plugin.collectors`, section 8). The operator's controls publish as
`task_web` and appear on the Machines page once used.

API at `/api/plugins/tasks`:

| Route | What |
|---|---|
| `GET /state` | what the Tickets page draws: tickets with readiness and priority, milestones, counts by state, whether the scheduler runs (the whole snapshot is the `snapshot` query on the bus) |
| `GET /board` | the Tasks panel: tickets in flight (held or offered) with their assignment (instance, since when), counts by state, capacity, pause, scheduler |
| `GET /attention` | the blocked tickets |
| `GET /tickets/{id}` | one ticket with its notes |
| `GET /recurring` | the recurring tickets (section 7): each definition's schedule in words, next due time (epoch and local), and last occurrence (id, outcome, ticket); `problems`: what is wrong with the definitions not used. Read-only; `/board` carries the same list for the Tasks panel |
| `GET /metrics` | the measurements as Prometheus text (section 8), the same text `tasking metrics` prints |
| `POST /control` | the operator's controls: `{action: pause\|resume}`, `{action: capacity, workers, reviewers}` (omit both to return to the file), `{action: kill, ticket}` (release whoever holds it, and the ticket goes back; or withdraw an offer nobody took) |

Every request reloads the configuration through Orchestra (`orchestra.toml` with the owner's
private overlay merged), so the capacity ceiling shown is the current one.

The pages, these routes and the static files are behind Orchestra's web login (Orchestra's SPEC
§10.2, PLUGINS.md §4.4.1): a browser's session, or `Authorization: Bearer <orchestra web token>`
from a program. `POST /control` carried by the session must come from Orchestra's own pages (a
same-origin `Origin` or `Referer`), or Orchestra answers `403` before the plugin sees it; the Tasks
panel's `fetch` sends it by itself. The plugin adds no check of its own.

These controls are the only writes a person has; tickets change only through the scheduler
(Orchestra's rule: humans act through machines, here a planner's request).

## 10. CLI (`tasking`)

| Command | What |
|---|---|
| `tasking init [--prefix P] [--unit-command C]` | write the task system's sections into `orchestra.toml` and register the project with Orchestra |
| `tasking run [--once] [--wait]` | the scheduler (`orchestra run` starts it with `--wait`; §4) |
| `tasking metrics` | the measurements as Prometheus text, from the record: the command the instrument `tasks.tickets` runs (§8) |
| `tasking status [--json]` | one screen: what needs a decision, scheduler, who holds what, queue; the gate in code mode, staging when configured |
| `tasking ticket list\|show ID`, `tasking milestone list\|show ID` | read the store |
| `tasking api [--as ROLE] OP [--args JSON]` | one request over the bus, answered by the running scheduler |
| `tasking staging status\|logs\|deploy\|restart\|stop` | staging |
| `tasking recurring check\|list [--json]` | the `[[tasks.recurring]]` definitions: `check` says what is wrong with each (exit 1 when anything is, milestones checked against the record), `list` their next due times and last occurrences |
| `tasking doctor [--fix]` | orphan worktrees; in code mode, missing gate specs |

`--repo` names the repository (default `ORCHESTRA_REPO`, else the current directory).

## 11. Running and testing

    uv sync --extra dev
    uv run demo/run.py                        # the demo at http://127.0.0.1:8071 (demo/README.md)
    uv run python -m pytest -q                # unit, e2e in-process (tests/fake_machine.py), e2e through a real Orchestra
    uv run ruff check tasking tests demo

`tests/unit` is fast and in-process. `tests/e2e/test_code_e2e.py` and `test_tasks_e2e.py` drive
the real scheduler, store, git and gate in-process with a protocol-speaking machine
(`tests/fake_machine.py`), including the code-mode gate flow the demo does not cover and the
contract's hard cases (leases, two machines answering, the operator's stop, restarts); the fake
announces what it listens for, as a real machine does. `test_handoffs_e2e.py` checks the hand-offs
Orchestra infers (planner → coordinator → worker → reviewer → worker → reviewer → merge_gate; a
gate rejection merge_gate → coordinator → worker; a held turn taken back worker → coordinator →
worker), that every offer is `coordinator/dispatch`'s, and that no event
has the source `task_scheduler` and none restates another machine's act; `tests/unit/test_measures.py`
checks the Prometheus text and that the scheduler writes no point;
`test_external_e2e.py` drives external review with the pull-request bridge's events published
by the test (feedback back to the same worker, a failing label, a close, stale feedback, a merge
while the worker has it, a restart, a review's comments gathered); `test_gate.py` and
`test_staging.py` drive the gate and staging directly; `test_demo_e2e.py` runs
the demo as processes against a real `orchestra run` and `orchestra web`, reads everything back
over HTTP, and executes the Tickets page in a headless Chrome when one is at hand (README).
