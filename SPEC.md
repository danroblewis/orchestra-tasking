# The task system

A task system observed by Orchestra. It holds tickets and milestones, decides which ticket is
worked next and by which role, offers that work over Orchestra's event bus, applies what comes
back, and in code mode lands approved branches through a merge gate. It is an Orchestra plugin
(`tasks`): its pages and API live inside Orchestra's web UI.

It runs no machine of its own besides its scheduler. Whoever fills the `worker` and `reviewer`
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
- **Only the scheduler changes a ticket's state**, from a report (section 3) or a planner's
  request (section 3). A planner is whatever decides what the work is: a person's script, a
  program, an agent in another package. The planner roles the task system names in its
  attention events are `supervisor` and `coordinator`; they are names, not programs here.
- Everything the task system does is **visible on the bus**: every offer, acceptance, report,
  request, answer and transition is a CloudEvent with the ticket as its subject, so Orchestra's
  Machines page shows who did what to which ticket without reading any payload.

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
`created_by` (the role that asked for it), a `version` for optimistic concurrency, and
timestamps. **Notes** record what happened: the worker's summary, the reviewer's verdict, the
gate's result, a blocker, a crash, a planner's note.

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
| active | `IN_PROGRESS` | held by a worker |
| | `IN_REVIEW` | held by a reviewer |
| | `MERGING` | in a gate run (code mode) |
| terminal | `DONE` | approved and, in code mode, landed on the base branch |
| | `CANCELLED` | a planner cancelled it |

### Transitions (`TRANSITIONS` in `model.py`)

| From | To | Cause |
|---|---|---|
| `OPEN`, `REJECTED` | `IN_PROGRESS` | a worker accepts the scheduler's offer (`worker_turns` + 1) |
| `IN_PROGRESS` | `READY_FOR_REVIEW` | the worker reports it; in code mode the branch head becomes `reviewed_commit` |
| `IN_PROGRESS` | `BLOCKED` | the worker reports it with a reason; or the worker went away three times in a row; or in code mode the branch has no commits |
| `IN_PROGRESS` | `OPEN` | the worker went away (lease ran out, no report, or a report the task system cannot use), the operator stopped it, or the scheduler restarted or shut down: offered again |
| `READY_FOR_REVIEW` | `IN_REVIEW` | a reviewer accepts the scheduler's offer |
| `READY_FOR_REVIEW` | `REJECTED` | the ticket's folder or worktree is missing at review time (`rejected_by = task_scheduler`; not a review round) |
| `IN_REVIEW` | `APPROVED` | the reviewer approves |
| `IN_REVIEW` | `REJECTED` | the reviewer rejects with feedback (`review_rounds` + 1); or four reviews in a row ended without a verdict (three were redone) |
| `IN_REVIEW` | `READY_FOR_REVIEW` | the reviewer went away or was stopped: reviewed again |
| `APPROVED` | `DONE` | tasks mode: nothing to merge |
| `APPROVED` | `MERGING` | code mode: the gate takes it |
| `MERGING` | `DONE` | the gate landed it |
| `MERGING` | `REJECTED` | the gate's tests failed, or the branch conflicts (`rejected_by = merge_gate`) |
| `MERGING` | `APPROVED` | the base branch itself is broken, or the gate hit an error: nothing attributed, the pipeline pauses |
| `BLOCKED` | `OPEN` | a planner unblocks it |
| any waiting state | `CANCELLED` | a planner cancels it (an active ticket must settle first) |

An offer nobody accepts is only a released assignment: no transition, nothing counted. When
`review_rounds` reaches `review_rounds_before_escalation` (and every multiple of it), the
coordinator role is asked to decide; nothing fails automatically. On start and on shutdown the
scheduler tells whoever holds work to stop (`task.released`), drops every assignment and moves
every active ticket to the waiting state before it, with a note: no zombies.

## 3. The contract on the bus

Everything crosses Orchestra's bus as CloudEvents (`tasking/events.py` over
`orchestra.events`). The ticket is the `subject`; the `source` is the machine (or
`<machine>/<instance>`). The task system's own sources are `task_scheduler` (the scheduler and its
desk), `task_web` (the operator's controls in the web UI) and `task_cli` (`tasking staging
deploy`); `tasking api` publishes as the role it is asked to be.

**Assignment.** The table in the docstring of `tasking/assignments.py` is the one source of
truth for `task.assigned`, `task.accepted`, `task.working`, `task.reported` and
`task.released`, with every field. In short: the scheduler publishes `task.assigned` to a role
with the ticket's brief and a workspace; the machine that takes it publishes `task.accepted`
with its instance; it publishes `task.working` every `HEARTBEAT_S` (30 s) while it works, and
`task.reported` with its report when done; the scheduler publishes `task.released` when the
lease (`LEASE_S`, 180 s without a word) runs out, when the operator stops the work, when another
instance already holds it, or when it restarts or shuts down. The first instance to accept holds
the ticket, which moves to `IN_PROGRESS` or `IN_REVIEW` with that instance as its `owner`; a
heartbeat or report from any other source is ignored. An offer nobody accepts within the lease
is released and offered again, and changed nothing: a role nobody fills is re-offered every
lease, visible as `task.assigned`/`task.released` pairs on the bus and as the growing "offered
to a worker Nm ago" on the Tasks panel (the age is the first offer's, kept by the scheduler in
memory, so a restart starts it over). The lease and the heartbeat are fixed, and every offer
says its `lease_s`: a machine accepts only what it can do within them.

**Requests** (`tasking/requests.py`). A planner changes tickets only by asking: `task.requested`
(create), `task.change_requested`, `task.note_requested`, `task.cancel_requested`,
`task.unblock_requested`, `milestone.requested`, `milestone.change_requested`,
`milestone.close_requested`, `capacity.change_requested`, `pipeline.resume_requested`; and reads
with `task.query_requested` naming the `op` (`list_tickets`, `get_ticket`, `project_status`,
`list_milestones`, `get_capacity`, `snapshot`, `list_gate_runs`, `get_gate_run`,
`get_staging_status`). Each carries `request` (an id), `args` and `by`: the label the asker
chooses, which names the author of what it makes (a change without one is refused). Which planner
may change what is the planners' own organisation; the task system checks the rules of tickets
and milestones, not who asks. The scheduler's desk applies it with `tasking/api.py` and answers
`request.done` {request, result} or `request.refused` {request, error}. `tasking api` sends one
such request from the command line; `requests.ask` does it from Python.

**Attention.** `task.attention_needed` {to, reason, ticket?} from the task system when a planner
should decide something, and only then: a ticket `BLOCKED`; a ticket rejected at every multiple
of `review_rounds_before_escalation`; the base branch broken; a gate error; the pipeline still
paused after a restart; a staging deploy failed. `to` is the planner role, `coordinator` or
`supervisor`. Routine creations, completions and landings are not attention:
`milestone.created`, `ticket.created`, `ticket.transitioned` and `gate.landed` carry them for
anyone listening. Nothing listens unless a planner machine does.

**Announcement.** Every machine publishes `orchestra.machine.started` when it starts (the
scheduler: `package: tasks`, `mode: continuous`, `kind: program`, what it `listens_for`, where
its `log` is). Orchestra's Machines page reads only those fields; the rest is passed to the
package's card. That announcement is the fact that the scheduler started; there is no
`scheduler.started` beside it.

**The names.** `task.*` is the protocol between the task system and the machines that fill roles
(assignment, requests, attention); `ticket.*` and `milestone.*` are the facts the task system
records about its own objects. So a request to create a ticket is `task.requested` and the fact
is `ticket.created`; `ticket.requested` is a candidate name at the next contract change.

**Observations.** The task system also publishes what happened, for Orchestra and anyone
listening, in families named up to the first dot (Orchestra's event map groups them so):
`ticket.created`, `ticket.updated`, `ticket.transitioned` {from, to, owner}, `ticket.note_added`,
`milestone.created`/`updated`/`closed`, `capacity.changed`, `pickup.paused`/`resumed`,
`kill.requested`, `pipeline.paused`/`resumed`, `probe.failed`/`recovered`,
`gate.started`/`conflict`/`landed`/`base_broken`/`rejected`/`error`,
`staging.deployed`/`failed`/`restarted`, `scheduler.stopped` (Orchestra has no
`machine.stopped`; that the scheduler stopped is a fact nothing else says). Orchestra never reads
their data. These families and the fields named here are part of what listeners may rely on,
like the assignment table and the request names: `ticket.transitioned` {ticket, from, to, owner},
`ticket.created` {ticket}, `gate.base_broken` {gate_run}, `probe.failed`/`probe.recovered`
{probe}, `scheduler.stopped`; rename them deliberately and say so in the commit.

## 4. The scheduler (`tasking/scheduler.py`, `tasking run`)

One process per project: a second `tasking run` for the same project exits, naming the pid that
runs it. On start it recovers (above). Every tick (`[tasks] tick`, 3 s by default) it:

1. reloads `orchestra.toml`, reaps finished threads, and for any ticket the operator asked to
   stop releases the assignment and sends the ticket back at once, counting it against no one;
2. **follows the bus**: the first acceptance names the holder, moves the ticket to the role's
   active state and starts the lease, a heartbeat extends it, a report is applied to the ticket
   and then the assignment is dropped (a report that cannot be applied still frees the ticket:
   back to waiting, with a note); then every lease that ran out is released: a holder that went
   quiet is a crash, an offer nobody took is dropped and offered again (every lease, for as
   long as nobody fills the role: `task.assigned`/`task.released` pairs on the bus, and the
   Tasks panel's "offered to a worker Nm ago" grows from the first offer);
3. **offers work**: reviews first (finished work lands before new work starts), then, unless
   pickup is paused, ready `OPEN` and `REJECTED` tickets in effective-priority order, up to the
   role's capacity. Capacity is the operator's ceiling from the Dashboard if set, otherwise
   `[tasks] workers`/`reviewers`, with a planner's dial (`set_capacity`) under it;
4. in code mode, starts a gate run for the oldest `APPROVED` ticket if none runs and the
   pipeline is not paused, and drops the worktrees of cancelled tickets;
5. keeps staging running (section 5) and runs the due **probes** (`tasking/probes.py`:
   `[[probes]]` commands on an interval; `probe.failed`/`probe.recovered` fire on the transition);
6. once per discovery step, writes the ticket levels again as they stand (section 8).

What a report makes of a ticket is `tasking/reports.py`. A report is usable only if a worker's
says `state` `READY_FOR_REVIEW`, or `BLOCKED` with a `blocked_reason`, and a reviewer's says
`verdict` `APPROVED` or `REJECTED`; anything else counts as no report, with what was wrong as the
machine's last words.

**Applying a worker's report.** `READY_FOR_REVIEW` moves the ticket on (code mode: uncommitted
changes are committed, and a branch with no commits beyond the base is `BLOCKED` instead).
`BLOCKED` records the reason and asks the coordinator. No report (the lease ran out, or the
report was unusable) is a crash: up to two in a row go back to `OPEN` with a note and are offered
again; the third blocks the ticket. `failed` true in the report means the machine went away or
its lease ran out, retried up to twice; `failed` false with a null report means it ended on
purpose without one, and the ticket is `BLOCKED` for a planner. An operator stop, a restart or
a shutdown goes back to `OPEN` without counting; an offer nobody accepted changes nothing. Notes
and feedback written by the task system are signed `task_scheduler` and state what happened.

**Applying a reviewer's report.** `APPROVED` moves to `APPROVED` (and in tasks mode straight to
`DONE`); `REJECTED` records the feedback and sends the ticket back, `rejected_by` the reviewer.
No verdict is not a rejection: the review is redone, up to three times, before the ticket is sent
back by the task system (`rejected_by` `task_scheduler`); the merge gate's send-backs say
`merge_gate`. In code mode a review whose preparation fails (the unit tests cannot run, git
fails) is not offered: the record is dropped and the ticket, still `READY_FOR_REVIEW`, is offered
again. A ticket whose folder or worktree is gone at review time is rejected by the task system
without a review round, since nothing was reviewed. On shutdown the scheduler lets a gate run
finish before it releases anything.

**Requests** are served by the desk, a thread in the same process (section 3). It reads on from
where the last desk stopped (the bus offset and the request ids answered from there, in the
runtime record), so a request made while no scheduler ran is answered when one starts: applied
if it is younger than twice what `ask` waits (`RESUME_S`, 60 s), refused with the reason if
older, since its asker gave up long ago and a change applied late would surprise everyone. A
restart answers no request twice. A machine's report for an assignment the restart dropped is
ignored, with a line in the log: the ticket was offered afresh. The scheduler's log is
`<state>/logs/scheduler.log`, which it announces so Orchestra can show it.

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
done or cancelled. A code worker's offer names the project's first `[[gate.unit]]` command. Before
a review is offered, the scheduler runs the unit specs on the branch and puts the output and the
commit in the offer (`review.test_output`, `review.commit`), so the reviewer sees the facts.

**The merge gate** (`tasking/gate.py`) lands approved code, one ticket per run: a gate
worktree on a temporary branch from the base head, `git merge --no-ff` of the reviewed commit (a
conflict sends the ticket back to its worker without counting as a review round), then the
stages from `orchestra.toml`: `[build]` (the artifact is copied to `<state>/artifacts/<run>/`),
every `[[gate.unit]]` spec, and if `[acceptance]` and `[[gate.acceptance]]` are set, the artifact
started on a free port and the acceptance specs run against it. All pass: the base branch is
fast-forwarded (pushed if `push = true`), the ticket is `DONE`, its worktree and branch removed,
and the build deployed to staging. A failure: the base is checked alone; if the base itself fails
the pipeline pauses and the supervisor role is told; otherwise the ticket is rejected with the
failing stage's output as feedback.

**Staging** (`tasking/staging.py`): when `[staging] start` is set, one long-lived instance of the
last build that passed the gate, started with the artifact and port as placeholders, probed at
`health`, restarted by the scheduler if its process dies, optionally behind a named cloudflared
tunnel (`[staging.tunnel]`). A failed deploy rolls back to the previous build and tells the
supervisor role. `tasking staging status|logs|deploy|restart|stop` operates it.

## 6. Writing a machine that fills a role

A machine is a program that reads Orchestra's bus, answers offers for its role, and says what it
did. It needs Orchestra as a library and nothing from this package; the contract is the five
event types of section 3. The demo's `demo/machines/shell_worker.py` and
`demo/machines/check_reviewer.py` are each about a page and are the reference. The shape:

1. **Open the bus as yourself.** `Events.for_project(load_project(), source="worker/1")`
   (`orchestra.config`, `orchestra.events`): the source is `<machine>/<instance>`, so Orchestra
   sees one machine `worker` with an instance `1`, and the scheduler knows who holds what.
   `load_project()` takes the repository from `ORCHESTRA_REPO`, else the current directory: run
   the machine from the repository, or set `ORCHESTRA_REPO` to it, as `demo/run.py` does.
2. **Announce yourself**: publish `orchestra.machine.started` with `mode` (`continuous`), `kind`
   (`program`), `listens_for` (`["task.assigned", "task.released"]`) and, if you keep one, `log`.
   The Machines page shows the machine from this and from what it then publishes.
3. **Follow the bus from now.** `offset = events.end()`, then in a loop read what is new
   (`evs, offset = events.after(offset)`) and look at `type` and `data`. Every
   `task.assigned` whose `data.role` is yours is an offer: the ticket's `brief` (title,
   description, acceptance, feedback from a rejection, …), the `workspace` to work in, and in
   code mode the `branch`.
4. **Accept**, only what you can do within the lease: the offer says its `lease_s`. A machine
   that is the only kind on its role may accept work it cannot do and report `BLOCKED`, so a
   planner hears of it (the demo's worker does this for a ticket without commands); where
   several kinds of machine serve one role, each accepts only what it can do, and the first to
   accept holds it. An offer nobody accepts is visible only as `task.released` on the bus and
   is re-offered each lease. To accept, publish `task.accepted` {assignment, ticket, role} with
   `subject=ticket`: the holder is the event's source; `instance`, if sent, must equal it (an
   acceptance whose instance is not its source is refused, and the offer stays open). If
   another instance was first you get `task.released` naming your instance: stop.
5. **Work, and say so**: do the task in the workspace; publish `task.working` {assignment}
   well within the offer's `lease_s` (`HEARTBEAT_S`, 30 s; the demo's worker says it every sixth
   of the lease) so the lease goes on (a review that takes a moment needs none). If
   `task.released` names your assignment, stop what you are doing, the heartbeat and the report
   with it: the ticket is no longer yours.
6. **Report**: publish `task.reported` {assignment, ticket, role, report, failed: false, text:
   ""}. A worker's report is `{state: READY_FOR_REVIEW | BLOCKED, summary, blocked_reason}`;
   a reviewer's is `{verdict: APPROVED | REJECTED, summary, feedback}`. The summary becomes a
   note on the ticket; the feedback is what the next worker is told; the blocked reason is what
   the planner reads in the banner.

A worker that cannot act on a rejection's feedback must report `BLOCKED` instead of redoing
identical work; otherwise a program worker and a program reviewer argue forever, with an
attention event at every multiple of the threshold and nothing else stopping them: the scheduler
re-offers a `REJECTED` ticket at once and has no floor of its own without a planner. The demo's
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
review_rounds_before_escalation = 3
aging_days_per_level = 2 # a ticket gains one priority level per N days of age; 0 disables

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

`[events]` and `[metrics]` are Orchestra's: the task system opens the bus and the metrics store
through Orchestra's own project (`tasking.config.Project.orchestra`), so where they go is decided
in one place. Capacity and the config are re-read every tick, so a change is live.

## 8. State

```
~/.orchestrator/<key>/                 # Orchestra's; <key> is <repo name>-<hash of its path>
  events.jsonl                         # the file bus (when [events] url is "file")
  tasks/                               # the task system's
    tickets.json                       # tickets, milestones, notes, counters, the metric counters
    runtime.json                       # scheduler pid, assignments, capacity, pause, kill requests,
                                       # worktrees, gate runs, builds, pipeline, probes, staging,
                                       # where the desk reads requests from next
    .lock                              # one file lock around every read-modify-write
    workspaces/<ticket>/               # tasks mode
    worktrees/<ticket>/                # code mode
    gate/<run>/  artifacts/<run>/  staging/
    logs/scheduler.log  logs/<run>/<stage>-<spec>.log  logs/review/<ticket>/
```

Only `tasking/store/` reads or writes these JSON files (`store/tickets.py` the first,
`store/runtime.py` the second, behind one `LocalStore`). Metrics go to Orchestra's metrics store
(`tasking/measures.py`): `tasks_tickets{state}`, `tasks_tickets_created_total{machine,by}`,
`tasks_ticket_transitions_total{from,to,machine,by}`, a level and the counters that move it from
the same change, so they balance; on the counters `machine` is the writer (`task_scheduler`)
and `by` the actor whose report or request it applied (`worker`, `reviewer`, a planner's
machine, or `task_scheduler` for its own moves). Between changes the scheduler writes the levels again every
discovery step (Orchestra's `[discovery] step`, 30 s by default), the counters untouched, so a
quiet system is seen measuring its stocks rather than taken for one whose writer is gone.

## 9. The plugin: pages and API (`tasking/web.py`, `tasking/static/`)

Inside Orchestra's web UI, under the plugin name `tasks`:

- **Tickets** (`/tickets`): the graph of milestones and tickets (dagre), done tickets collapsed
  into "via n done" edges, a drawer per ticket (what needs attention first: the blocker, the
  rejection, why it waits; then its fields, texts and notes). The gate's result is on the ticket
  as the `merge_gate` note and feedback.
- **The Tasks panel** on the Dashboard (`tasks-panel.js`): the tickets in flight with the
  instance that holds each and since when, or to whom it is offered, the queue by state, the
  capacity and pause controls.
- **The scheduler's card** (`task_scheduler`, the task queue) on the Machines page and Dashboard
  (`machine-cards.js`): the queue by state and the same controls.
- **The blocked-ticket banner** on every page (`attention.js`): which tickets wait for a
  decision; a package that shows them itself calls `TaskAttention.standAside()`.

`tasks-boot.js` is the one script Orchestra loads on every page: it loads `tasks.js` (the shared
code and the controls) and then the three above. The look every page needs, `tasks.css`, is a
stylesheet the plugin declares (`stylesheets`), which Orchestra links in every page's head. The
Tickets page adds its own `tickets.js` (the graph) and `tickets.css`. The plugin declares one
machine, `task_scheduler`; the operator's controls publish as `task_web` and appear on the
Machines page once used.

API at `/api/plugins/tasks`:

| Route | What |
|---|---|
| `GET /state` | what the Tickets page draws: tickets with readiness and priority, milestones, counts by state, whether the scheduler runs (the whole snapshot is the `snapshot` query on the bus) |
| `GET /board` | the Tasks panel: tickets in flight (held or offered) with their assignment (instance, since when), counts by state, capacity, pause, scheduler |
| `GET /attention` | the blocked tickets |
| `GET /tickets/{id}` | one ticket with its notes |
| `POST /control` | the operator's controls: `{action: pause\|resume}`, `{action: capacity, workers, reviewers}` (omit both to return to the file), `{action: kill, ticket}` (release whoever holds it, and the ticket goes back; or withdraw an offer nobody took) |

Every request re-reads the task system's sections of `orchestra.toml`, so the capacity ceiling
shown is the file's current one.

These controls are the only writes a person has; tickets change only through the scheduler
(Orchestra's rule: humans act through machines, here a planner's request).

## 10. CLI (`tasking`)

| Command | What |
|---|---|
| `tasking init [--prefix P] [--unit-command C]` | write the task system's sections into `orchestra.toml` and register the project with Orchestra |
| `tasking run [--once]` | the scheduler |
| `tasking status [--json]` | one screen: what needs a decision, scheduler, who holds what, queue; the gate in code mode, staging when configured |
| `tasking ticket list\|show ID`, `tasking milestone list\|show ID` | read the store |
| `tasking api [--as ROLE] OP [--args JSON]` | one request over the bus, answered by the running scheduler |
| `tasking staging status\|logs\|deploy\|restart\|stop` | staging |
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
contract's hard cases (leases, two machines answering, the operator's stop, restarts);
`test_gate.py` and `test_staging.py` drive the gate and staging directly; `test_demo_e2e.py` runs
the demo as processes against a real `orchestra run` and `orchestra web`, reads everything back
over HTTP, and executes the Tickets page in a headless Chrome when one is at hand (README).
