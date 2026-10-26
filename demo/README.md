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
| `orchestra web --port 8071` | Orchestra's web UI, with the task system's Tickets page, Tasks panel and scheduler card (its plugin) |
| `tasking run` | the scheduler: offers ready tickets to the roles over the bus, applies what comes back |
| `machines/shell_worker.py` | the machine `worker` (instance `worker/1`): runs the `sh` code blocks of a ticket's description in its workspace (the ticket's own empty folder; `ORCHESTRA_REPO` names the repository), writes RESULT.md |
| `machines/check_reviewer.py` | the machine `reviewer` (`reviewer/1`): approves when RESULT.md exists and the acceptance text's `` `files` `` and `"lines"` are there |

The two machines are each a page of Python that imports only Orchestra (`orchestra.config`,
`orchestra.events`) and speaks the contract in `tasking/assignments.py`. They are the example of
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

Where to look, at http://127.0.0.1:8071:

- **Dashboard**: the Tasks panel (cards for the tickets in flight, which instance holds each or
  what it waits for, the queue by state, the capacity and pause controls); the Machines panel
  lists the scheduler as one line; the Core strip shows the host, bus and metrics store up.
- **Tickets**: the graph of milestones and tickets; done tickets are hidden, so click the
  milestone (2/4 done) or tick "show done tickets" to see DEMO-1 and DEMO-2. Click a ticket for
  its description, acceptance, notes (the worker's summary, the reviewer's verdict) and the
  blocker or feedback.
- **Machines**: `task_scheduler` (package tasks, with the task queue card), `worker` and
  `reviewer` as observed from the bus (each announces where its log is, so its dialog shows
  it), `planner` (run.py's one-shot requests, a scheduled program) and `orchestra-host`; what
  each publishes and listens for, and the event map between
  them: `task.assigned` from the scheduler, `task.accepted`/`task.reported` from the machines,
  `task.requested` from the planner, `request.done` back. `task_web` appears once an operator
  control has been pressed.
- **Dynamics**: a thin model, hidden at first. Seconds after the start the host saves a first
  version with one stock, `tasks tickets (OPEN)` (the series `tasks_tickets{state="OPEN"}`), fed
  by one flow, `tasks tickets created` (the counter `tasks_tickets_created_total`), attributed to
  `task_scheduler`. A stock that only fills starts hidden, so the page looks empty until the
  "stocks that only fill (1)" box is ticked. Discovery looks at a 60-minute window on a 5-second
  grid (`[discovery]` in orchestra.toml: a shell ticket lives seconds, so at the usual 30 s its
  whole life would fit inside one step), and the demo's twenty seconds of ticket changes give it
  nothing more. A meaningful model is shown on the sample project, not here.
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

Logs: `run/host.log`, `run/web.log`, `run/scheduler.log` (the scheduler's stdout),
`run/worker.log`, `run/reviewer.log` in the target; and
`~/.orchestrator/<name>-<hash>/tasks/logs/scheduler.log`, the scheduler's own log, the one the
Machines page opens.
