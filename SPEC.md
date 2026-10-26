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
  holds the work and a report for its verdict, nothing more. No sessions, transcripts, prompts
  or harnesses are here; the package that runs AI agents (orchestra-agents) is a machine like
  any other and imports nothing from this one.
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
| active | `IN_PROGRESS` | offered to or held by a worker |
| | `IN_REVIEW` | offered to or held by a reviewer |
| | `MERGING` | in a gate run (code mode) |
| terminal | `DONE` | approved and, in code mode, landed on the base branch |
| | `CANCELLED` | a planner cancelled it |

### Transitions (`TRANSITIONS` in `model.py`)

| From | To | Cause |
|---|---|---|
| `OPEN`, `REJECTED` | `IN_PROGRESS` | the scheduler offers it to a worker (`worker_turns` + 1) |
| `IN_PROGRESS` | `READY_FOR_REVIEW` | the worker reports it; in code mode the branch head becomes `reviewed_commit` |
| `IN_PROGRESS` | `BLOCKED` | the worker reports it with a reason; or the worker went away three times in a row; or in code mode the branch has no commits |
| `IN_PROGRESS` | `OPEN` | the worker went away (lease ran out, no report), the operator stopped it, or the scheduler restarted: offered again |
| `READY_FOR_REVIEW` | `IN_REVIEW` | the scheduler offers it to a reviewer |
| `IN_REVIEW` | `APPROVED` | the reviewer approves |
| `IN_REVIEW` | `REJECTED` | the reviewer rejects with feedback (`review_rounds` + 1); or three reviews in a row ended without a verdict |
| `IN_REVIEW` | `READY_FOR_REVIEW` | the reviewer went away or was stopped: reviewed again |
| `APPROVED` | `DONE` | tasks mode: nothing to merge |
| `APPROVED` | `MERGING` | code mode: the gate takes it |
| `MERGING` | `DONE` | the gate landed it |
| `MERGING` | `REJECTED` | the gate's tests failed, or the branch conflicts (`rejected_by = merge_gate`) |
| `MERGING` | `APPROVED` | the base branch itself is broken, or the gate hit an error: nothing attributed, the pipeline pauses |
| `BLOCKED` | `OPEN` | a planner unblocks it |
| any waiting state | `CANCELLED` | a planner cancels it (an active ticket must settle first) |

When `review_rounds` reaches `review_rounds_before_escalation` (and every multiple of it), the
coordinator role is asked to decide; nothing fails automatically. On start the scheduler moves
every active ticket to the waiting state before it and drops every assignment: no zombies.

## 3. The contract on the bus

Everything crosses Orchestra's bus as CloudEvents (`tasking/events.py` over
`orchestra.events`). The ticket is the `subject`; the `source` is the machine (or `<machine>/<instance>`).

**Assignment.** The table in the docstring of `tasking/assignments.py` is the one source of
truth for `task.assigned`, `task.accepted`, `task.working`, `task.reported` and
`task.released`, with every field. In short: the scheduler publishes `task.assigned` to a role
with the ticket's brief and a workspace; the machine that takes it publishes `task.accepted`
with its instance; it publishes `task.working` every `HEARTBEAT_S` (30 s) while it works, and
`task.reported` with its report when done; the scheduler publishes `task.released` when the
lease (`LEASE_S`, 180 s without a word) runs out, when the operator stops the work, when another
instance already holds it, or when it shuts down. The first instance to accept holds the ticket
and becomes its `owner`.

**Requests** (`tasking/requests.py`). A planner changes tickets only by asking: `task.requested`
(create), `task.change_requested`, `task.note_requested`, `task.cancel_requested`,
`task.unblock_requested`, `milestone.requested`, `milestone.change_requested`,
`milestone.close_requested`, `capacity.change_requested`, `pipeline.resume_requested`; and reads
with `task.query_requested` naming the `op` (`list_tickets`, `get_ticket`, `project_status`,
`list_milestones`, `get_capacity`, `snapshot`, `list_gate_runs`, `get_gate_run`,
`get_staging_status`). Each carries `request` (an id), `args` and `by` (the role). The
scheduler's desk applies it with `tasking/api.py` and answers `request.done` {request, result}
or `request.refused` {request, error}. `tasking api` sends one such request from the command
line; `requests.ask` does it from Python.

**Attention.** `task.attention_needed` {to, reason, ticket?} from the scheduler when a planner
should look: a ticket blocked, rejected again and again, done; a ticket or milestone created by
someone else; the base branch broken; the pipeline paused. `to` is the planner role,
`coordinator` or `supervisor`. Nothing listens unless a planner machine does.

**Announcement.** Every machine publishes `orchestra.machine.started` when it starts (the
scheduler: `package: tasks`, `mode: continuous`, what it `listens_for`, where its `log` is).
Orchestra's Machines page reads only those fields; the rest is passed to the package's card.

**Observations.** The scheduler also publishes what happened, for Orchestra and anyone
listening: `ticket_created`, `ticket_updated`, `ticket_transition` {from, to, owner},
`note_added`, `milestone_*`, `capacity_changed`, `pickup_paused`/`pickup_resumed`,
`kill_requested`, `pipeline_paused`/`pipeline_resumed`, `probe_failed`/`probe_recovered`,
`gate_*`, `staging_*`, `loop_started`/`loop_stopped`. Orchestra never reads their data.

## 4. The scheduler (`tasking/loop.py`, `tasking run`)

One process per project. Every 3 s it:

1. reloads `orchestra.toml`, reaps finished threads, and releases the work of any ticket the
   operator asked to stop;
2. **follows the bus**: an acceptance names the owner and starts the lease, a heartbeat extends
   it, a report drops the assignment and is applied to the ticket; then every lease that ran out
   is released and applied as a crash;
3. **offers work**: reviews first (finished work lands before new work starts), then, unless
   pickup is paused, ready `OPEN` and `REJECTED` tickets in effective-priority order, up to the
   role's capacity. Capacity is the operator's ceiling from the Dashboard if set, otherwise
   `[tasks] workers`/`reviewers`, with a planner's dial (`set_capacity`) under it;
4. in code mode, starts a gate run for the oldest `APPROVED` ticket if none runs and the
   pipeline is not paused, and drops the worktrees of cancelled tickets;
5. keeps staging running (section 5) and runs the due **probes** (`tasking/probes.py`:
   `[[probes]]` commands on an interval; `probe_failed`/`probe_recovered` fire on the transition).

**Applying a worker's report.** `READY_FOR_REVIEW` moves the ticket on (code mode: uncommitted
changes are committed, and a branch with no commits beyond the base is `BLOCKED` instead).
`BLOCKED` records the reason and asks the coordinator. No report (the lease ran out) is a crash:
up to two in a row go back to `OPEN` with a note and are offered again; the third blocks the
ticket. An operator stop or a scheduler shutdown goes back to `OPEN` without counting.

**Applying a reviewer's report.** `APPROVED` moves to `APPROVED` (and in tasks mode straight to
`DONE`); `REJECTED` records the feedback and sends the ticket back. No verdict is not a
rejection: the review is redone, up to three times, before the ticket is sent back by the
scheduler with that reason.

**Requests** are served by the desk, a thread in the same process (section 3). The scheduler's
log is `<state>/logs/scheduler.log`, which it announces so Orchestra can show it.

## 5. Work modes: tasks and code

`[project] work` chooses what a ticket's workspace is.

**`tasks`** (the default): the workspace is a folder, `<state>/workspaces/<ticket>/`, made when
the ticket is first offered and kept for the reviewer and for any further turn. Nothing is
merged; approval is done. The result of a task is whatever the worker left there (the demo's
worker writes `RESULT.md`) and its report.

**`code`**: the workspace is a git worktree at `<state>/worktrees/<ticket>/` on a branch
`orchestra/<ticket>-<slug>` from `base_branch`, reused by every turn, removed when the ticket is
done or cancelled. A code worker's offer names the project's first `[[gate.unit]]` command. Before
a review is offered, the scheduler runs the unit specs on the branch and puts the output and the
commit in the offer (`review.test_output`, `review.commit`), so the reviewer sees the facts.

**The merge gate** (`tasking/gate/`) lands approved code, one ticket per run today: a gate
worktree on a temporary branch from the base head, `git merge --no-ff` of the reviewed commit (a
conflict sends the ticket back to its worker without counting as a review round), then the
stages from `orchestra.toml`: `[build]` (the artifact is copied to `<state>/artifacts/<run>/`),
every `[[gate.unit]]` spec, and if `[acceptance]` and `[[gate.acceptance]]` are set, the artifact
started on a free port and the acceptance specs run against it. All pass: the base branch is
fast-forwarded (pushed if `push = true`), the ticket is `DONE`, its worktree and branch removed,
and the build deployed to staging. A failure: the base is checked alone; if the base itself fails
the pipeline pauses and the supervisor role is told; otherwise the ticket is rejected with the
failing stage's output as feedback. `gate/bisect.py` is the pure bisection over a batch, unit
tested and not yet wired: batches (`[gate] batch_size`, `batch_wait`) are read from the config
and not yet used.

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
2. **Announce yourself**: publish `orchestra.machine.started` with `mode` (`continuous`), `kind`
   (`program`), `listens_for` (`["task.assigned", "task.released"]`) and, if you keep one, `log`.
   The Machines page shows the machine from this and from what it then publishes.
3. **Follow the bus from now.** `offset = events.end()`, then in a loop read what is new
   (`events.read(offset, limit=end - offset)`) and look at `type` and `data`. Every
   `task.assigned` whose `data.role` is yours is an offer: the ticket's `brief` (title,
   description, acceptance, feedback from a rejection, …), the `workspace` to work in, and in
   code mode the `branch`.
4. **Accept**: publish `task.accepted` {assignment, ticket, role, instance} with
   `subject=ticket`. If another instance was first you get `task.released` naming your instance:
   stop.
5. **Work, and say so**: do the task in the workspace; every 30 s publish `task.working`
   {assignment} so the lease goes on (a review that takes a moment needs none). If
   `task.released` names your assignment, stop what you are doing: the ticket is no longer yours.
6. **Report**: publish `task.reported` {assignment, ticket, role, report, failed: false, text:
   ""}. A worker's report is `{state: READY_FOR_REVIEW | BLOCKED, summary, blocked_reason}`;
   a reviewer's is `{verdict: APPROVED | REJECTED, summary, feedback}`. The summary becomes a
   note on the ticket; the feedback is what the next worker is told; the blocked reason is what
   the planner reads in the banner.

What a worker cannot do, it says: the demo's worker reports `BLOCKED` when a ticket gives no
commands, and when a rejected ticket would run the same commands again, because a program cannot
act on a reviewer's words. A reviewer that cannot judge a criterion says so in its summary. The
scheduler never needs to know why; it moves the ticket and asks the planner.

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
stop = ""

[gate]
timeout = "900s"         # per spec
batch_size = 6           # read, not yet used: one ticket per run today
batch_wait = "120s"

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
every = "5m"
timeout = "60s"
```

`[events]` and `[metrics]` are Orchestra's; the task system reads them only to reach the same
bus and metrics store. Capacity and the config are re-read every tick, so a change is live.

## 8. State

```
~/.orchestrator/<key>/                 # Orchestra's; <key> is <repo name>-<hash of its path>
  events.jsonl                         # the file bus (when [events] url is "file")
  tasks/                               # the task system's
    tickets.json                       # tickets, milestones, notes, counters, the metric counters
    runtime.json                       # scheduler pid, assignments, capacity, pause, kill requests,
                                       # worktrees, gate runs, builds, pipeline, probes, staging
    .lock                              # one file lock around every read-modify-write
    workspaces/<ticket>/               # tasks mode
    worktrees/<ticket>/                # code mode
    gate/<run>/  artifacts/<run>/  staging/
    logs/scheduler.log  logs/<run>/<stage>-<spec>.log  logs/review/<ticket>/
```

Only `tasking/store/` reads or writes these JSON files. Metrics go to Orchestra's metrics store
(`tasking/measures.py`): `tasks_tickets{state}`, `tasks_tickets_created_total{machine}`,
`tasks_ticket_transitions_total{from,to,machine}`, a level and the counters that move it from
the same change, so they balance.

## 9. The plugin: pages and API (`tasking/web.py`, `tasking/static/`)

Inside Orchestra's web UI, under the plugin name `tasks`:

- **Tickets** (`/tickets`): the graph of milestones and tickets (dagre), done tickets collapsed
  into "via n done" edges, a drawer per ticket (what needs attention first: the blocker, the
  rejection, why it waits; then its fields, texts and notes) and per gate run.
- **The Tasks panel** on the Dashboard (`tasks-panel.js`): the tickets in flight with who holds
  each and since when, the queue by state, the capacity and pause controls.
- **The scheduler's card** on the Machines page and Dashboard (`machine-cards.js`): the queue by
  state and the same controls.
- **The blocked-ticket banner** on every page (`attention.js`): which tickets wait for a
  decision; a package that shows them itself calls `TaskAttention.standAside()`.

API at `/api/plugins/tasks`:

| Route | What |
|---|---|
| `GET /state` | the snapshot the Tickets page draws: tickets with readiness and effective priority, milestones, assignments, capacity, pipeline, probes, gate runs, whether the scheduler runs |
| `GET /board` | the Tasks panel: tickets in flight with their assignment, counts by state, capacity, pause, scheduler |
| `GET /attention` | the blocked tickets |
| `GET /tickets/{id}` | one ticket with its notes and worktree |
| `GET /gate/runs/{id}` | one gate run with its stages |
| `POST /control` | the operator's controls: `{action: pause\|resume}`, `{action: capacity, workers, reviewers}` (omit both to return to the file), `{action: kill, ticket}` (release whoever holds it; the ticket goes back) |

These controls are the only writes a person has; tickets change only through the scheduler
(Orchestra's rule: humans act through machines, here a planner's request).

## 10. CLI (`tasking`)

| Command | What |
|---|---|
| `tasking init [--prefix P] [--unit-command C]` | write the task system's sections into `orchestra.toml` and register the project with Orchestra |
| `tasking run [--once]` | the scheduler |
| `tasking status [--json]` | one screen: scheduler, who holds what, gate, queue, staging |
| `tasking ticket list\|show ID`, `tasking milestone list\|show ID` | read the store |
| `tasking api [--as ROLE] OP [--args JSON]` | one request over the bus, answered by the running scheduler |
| `tasking staging status\|logs\|deploy\|restart\|stop` | staging |
| `tasking doctor [--fix]` | orphan worktrees, missing gate specs |

`--repo` names the repository (default `ORCHESTRA_REPO`, else the current directory).

## 11. Running and testing

    uv sync --extra dev
    uv run demo/run.py                        # the demo at http://127.0.0.1:8071 (demo/README.md)
    uv run python -m pytest -q                # unit, e2e in-process (tests/fake_machine.py), e2e through a real Orchestra
    uv run ruff check tasking tests demo

`tests/unit` is fast and pure. `tests/e2e/test_loop_e2e.py` and `test_tasks_e2e.py` drive the
real scheduler, store, git and gate in-process with a protocol-speaking machine
(`tests/fake_machine.py`), including the code-mode gate flow the demo does not cover;
`test_gate.py` and `test_staging.py` drive the gate and staging directly;
`test_demo_e2e.py` runs the demo as processes against a real `orchestra web` and reads
everything back over HTTP.
