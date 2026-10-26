# The task system's demo

A project with nothing of its own: Orchestra, the task system (orchestra-tasking) and two
plain programs that fill its roles: the worker runs shell commands, the reviewer checks files
and lines. From the orchestra-tasking repository:

    uv run demo/run.py                 # copies this folder to ~/orchestra-tasking-demo and starts everything
    uv run demo/run.py /tmp/d --port 8071

Ctrl-C stops everything. Remove the target directory to run it again (its state is under
`~/.orchestrator/<name>-<hash>/`; `tasks/workspaces/DEMO-n/` holds each ticket's RESULT.md).

## What runs

| Process | What it is |
|---|---|
| `orchestra web --port 8071` | Orchestra's web UI, with the task system's Tickets page, Tasks panel and scheduler card (its plugin) |
| `tasking run` | the scheduler: offers ready tickets to the roles over the bus, applies what comes back |
| `machines/shell_worker.py` | the machine `worker` (instance `worker/1`): runs the ```` ```sh ```` blocks of a ticket's description in its workspace, writes RESULT.md |
| `machines/check_reviewer.py` | the machine `reviewer` (`reviewer/1`): approves when RESULT.md exists and the acceptance text's `` `files` `` and `"lines"` are there |

The two machines are each a page of Python that imports only Orchestra (`orchestra.config`,
`orchestra.events`) and speaks the contract in `tasking/assignments.py`. They are the example of
how to write a machine that takes work from the task system.

## What you will see

`run.py` creates one milestone and four tickets as a planner would (requests over the bus):

- **DEMO-1 Say hello** and **DEMO-2 Report the Python version** (which depends on DEMO-1) go
  OPEN → IN_PROGRESS → READY_FOR_REVIEW → IN_REVIEW → APPROVED → DONE within a minute.
- **DEMO-3 Decide the project's name** has no commands: the worker reports it BLOCKED. A banner
  on every page says a ticket waits for a decision.
- **DEMO-4 Write today's date** asks for a file nothing writes: the reviewer rejects it with the
  missing file named, the worker is offered it again, sees that the same commands would give the
  same result, and reports it BLOCKED with the reviewer's words.

Where to look, at http://127.0.0.1:8071:

- **Dashboard**: the Tasks panel (cards for the tickets in flight, who holds each, the queue by
  state, the capacity and pause controls) and the Task queue card of the scheduler.
- **Tickets**: the graph of milestones and tickets; click one for its description, acceptance,
  notes (the worker's summary, the reviewer's verdict) and the blocker or feedback.
- **Machines**: `task_scheduler` (package tasks), `worker` and `reviewer` as observed from the
  bus, what each publishes and listens for, and the event map between them: `task.assigned`
  from the scheduler, `task.accepted`/`task.reported` from the machines, `task.requested` from
  the planner, `request.done` back.
- **Core**: the event log itself.

Logs: `run/web.log`, `run/scheduler.log`, `run/worker.log`, `run/reviewer.log` in the target.
