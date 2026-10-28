# The task system's demo

A project with nothing of its own: Orchestra, the task system (orchestra-tasking) and two
plain programs that fill its roles: the worker runs shell commands, the reviewer checks files
and lines. From the orchestra-tasking repository:

    uv run demo/run.py                 # copies this folder to ~/orchestra-tasking-demo and starts everything
    uv run demo/run.py /tmp/d --port 8071

Ctrl-C stops everything. To start over, remove the target directory and run it again: `run.py`
removes the state a previous run left for that path (under `~/.orchestrator/<name>-<hash>/`;
`tasks/workspaces/DEMO-n/` holds each ticket's RESULT.md) before it starts.

## What runs

| Process | What it is |
|---|---|
| `orchestra run` | Orchestra's host: the Core strip shows it up, and it rediscovers the system dynamics model from the metrics |
| `orchestra web --port 8071` | Orchestra's web UI, with the task system's Tickets page and Tasks panel (its plugin) |
| `tasking run --wait` | the scheduler: offers ready tickets to the roles over the bus, applies what comes back. `orchestra run` starts it (the task system's plugin declares it; `tasks.scheduler` on the Core page). It is no machine: what it publishes goes out as the machine it acts for (an offer as `coordinator/dispatch`: dispatch is the coordinator's), so the Machines page does not list it |
| `tasking metrics` | the instrument `tasks.tickets`: `orchestra run` runs it every discovery step (the plugin declares it) and writes the ticket levels and transition counters it prints to the metrics store |
| `python -m tasking.machines.shell_worker` | the machine `worker` (instance `worker/1`): runs the `sh` code blocks of a ticket's description in its workspace (the ticket's own empty folder; `ORCHESTRA_REPO` names the repository), writes RESULT.md |
| `python -m tasking.machines.check_reviewer` | the machine `reviewer` (`reviewer/1`): approves when RESULT.md exists and the acceptance text's `` `files` `` and `"lines"` are there (in code mode: when a check command passes in the ticket's worktree) |

`run.py` starts only `orchestra run` and `orchestra web`; `orchestra run` starts the scheduler, the
instrument and the two machines (declared under `[[machines]]` in this folder's `orchestra.toml`) and keeps them
running.

The two machines are each a page of Python that imports only Orchestra (`orchestra.config`,
`orchestra.events`), shipped in the task system's package (`tasking/machines/shell_worker.py`,
`tasking/machines/check_reviewer.py`) so that other projects install them rather than copy
them. Both speak the contract in `tasking/assignments.py`. They are the example of
how to write a machine that takes work from the task system.

## What you will see

`run.py` creates one milestone and four tickets as a planner would (requests over the bus). The
whole run takes about half a minute; DEMO-1 sleeps for ten seconds (a `sleep` standing for work,
the rule in the repository's README), so the Tasks panel shows `worker/1 · since Ns` on its card
before the end state (the reviewer's turn is over within one poll, too short to see). Were a role unfilled, its offers would be re-offered every lease
(`task.assigned`/`task.released` pairs on the bus) and the card would say "offered to a worker
Nm ago", growing from the first offer.

- **DEMO-1 Say hello** and **DEMO-2 Count the lines in the project's README** (which depends on
  DEMO-1) went OPEN → IN_PROGRESS → READY_FOR_REVIEW → IN_REVIEW → APPROVED → DONE. DEMO-1 makes
  its own file in the ticket's folder; DEMO-2 is about the repository, so its command goes there
  first (`cd "$ORCHESTRA_REPO" && …`): the rule for writing tickets is in the repository's
  README.
- **DEMO-3 Decide the project's name** has no commands: the worker reported it BLOCKED, saying
  so. A banner on every page says a ticket waits for a decision.
- **DEMO-4 Write today's date** asks for a file nothing writes: the reviewer rejected it with the
  missing file named, the worker was offered it again, saw that the same commands would give the
  same result, and reported it BLOCKED: the reviewer wants what the commands never produce, with
  the reviewer's words quoted.

Where to look, at http://127.0.0.1:8071 (open the `log in:` link `run.py` prints first: every page
asks for the project's web token):

- **Dashboard**: the Tasks panel (cards for the tickets in flight, which instance holds each or
  what it waits for, the queue by state, the capacity and pause controls); the Core strip shows
  the host, bus and metrics store up.
- **Tickets**: the graph of milestones and tickets; done tickets are hidden, so click the
  milestone (2/4 done) or tick "show done tickets" to see DEMO-1 and DEMO-2. Click a ticket for
  its description, acceptance, notes (the worker's summary, the reviewer's verdict) and the
  blocker or feedback.
- **Machines**: `worker` and `reviewer` as observed from the bus (each announces where its log
  is, so its dialog shows it), `planner` (run.py's one-shot requests, a scheduled program),
  `coordinator` (one instance, `dispatch`: the task system's queue, which the scheduler runs) and
  `orchestra-host`; what each publishes and listens for, and the event map between them:
  `task.requested` from the planner and `ticket.created` and `request.done` back as the planner
  (the scheduler answers for it), the hand-offs planner → coordinator via `ticket.created` and
  coordinator → worker via `task.assigned`, `task.accepted`
  from the machines, `review.requested` from the worker straight to the reviewer,
  `changes.requested` from the reviewer straight back to the worker. The scheduler is not
  there: it is no machine. `task_web` appears once an operator control has been pressed.
- **Dynamics**: no model. With four tickets every series has fewer than five events, or
  changes in fewer than five steps, which is discovery's bar for a stock or a flow (fewer
  coincide by chance), so the page shows no model; "how it was found" lists each series with
  why it was left out, and that list is the honest view of this run. Discovery looks at a
  60-minute window on a 5-second grid (`[discovery]` in orchestra.toml: a shell ticket lives
  seconds, so at the usual 30 s its whole life would fit inside one step). A longer run shows a
  model (orchestra-agents' demo is one).
- **Core**: the event log itself.

## To go on

The two blocked tickets wait for a planner: the attention events name a planner role
(`coordinator`), and nothing in the demo fills it, so the two requests below are what a planner
would send. DEMO-4 is unblocked by dropping the criterion nothing can meet and saying so. The
target is a copy of this folder with no environment of its own, so the commands run from the
orchestra-tasking checkout's environment, pointed at the target:

    cd ~/orchestra-tasking-demo && uv run --project ~/orchestra-tasking tasking api --as planner update_ticket --args '{"id": "DEMO-4", "acceptance": "- `today.txt` exists"}'
    cd ~/orchestra-tasking-demo && uv run --project ~/orchestra-tasking tasking api --as planner unblock_ticket --args '{"id": "DEMO-4", "note": "dropped the impossible criterion"}'

(or, from the orchestra-tasking checkout, `ORCHESTRA_REPO=~/orchestra-tasking-demo uv run
tasking api …`).

The ticket goes back to OPEN with the planner's note as its feedback, the scheduler offers it to
the worker again, the worker runs the same commands (the ticket changed, so it does not block),
the reviewer finds `today.txt`, and DEMO-4 is DONE. DEMO-3 needs commands in its description
(`update_ticket` with a `description`) before an unblock would get anywhere.

Logs: `run/host.log` and `run/web.log` in the target; what `orchestra run` starts writes to
`~/.orchestrator/<name>-<hash>/logs/machines/` (`tasks.scheduler.log`, `worker.log`,
`reviewer.log`; the Core page shows them); and
`~/.orchestrator/<name>-<hash>/tasks/logs/scheduler.log`, the scheduler's own log (no machine
on the Machines page opens it, since the scheduler is none; the demo runs in tasks mode, so there
is no `merge_gate`, whose announcement names it in code mode).
