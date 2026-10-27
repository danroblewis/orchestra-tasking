# orchestra-tasking

A task system observed by Orchestra. Tickets and milestones, a scheduler that offers ready
tickets to the `worker` and `reviewer` roles over Orchestra's event bus and applies what the
machines that take them report, a merge gate for code, and the Tickets page and Tasks panel
inside Orchestra's web UI (an Orchestra plugin, `tasks`).

How it works, with diagrams and the full contract for workers and reviewers: [ARCHITECTURE.md](ARCHITECTURE.md).

It runs no machine of its own. Whatever fills a role is another program that speaks the
contract in `tasking/assignments.py`: the demo's are a shell runner and a file checker, each a
page of Python using only Orchestra's library (`demo/machines/`). `SPEC.md` is the design;
`AGENTS.md` the rules.

## Try it

    uv sync --extra dev                # first clone github.com/danroblewis/orchestra beside this repository as `orchestra_2` (the path in [tool.uv.sources])
    uv run demo/run.py                 # a fresh demo project at ~/orchestra-tasking-demo, http://127.0.0.1:8071

Four tickets are created and worked by the two programs; in about half a minute two are done
and two are blocked for a planner to decide. `demo/README.md` says what you will see and where.
Ctrl-C stops everything; remove `~/orchestra-tasking-demo` first to run it again.

## Use it on a repository

The commands run from this repository's environment (`--project <this repo>`), since the target
repository need not have Orchestra or the task system installed:

    cd <repo> && uv run --project <this repo> tasking init     # the task system's sections in orchestra.toml
    uv run --project <this repo> orchestra web                 # Orchestra's UI with the Tickets page
    uv run --project <this repo> orchestra run                 # Orchestra's host; it starts the scheduler
    uv run --project <this repo> tasking api --as planner create_milestone --args '{"name": "First"}'
    uv run --project <this repo> tasking api --as planner create_ticket --args '{"title": "…", "milestone_id": "M1", "description": "…", "acceptance": "…"}'

Then run something that fills the roles: the demo's programs (`demo/machines/` as they are),
your own, or any other package's machines that speak the same contract over the bus
(orchestra-agents is one). Settings are the `[project]`, `[tasks]`, `[gate]`, … sections of `orchestra.toml`; state is in
`~/.orchestrator/<key>/tasks/`; events go on Orchestra's bus. If your tasks live seconds, set
`[discovery] step = "5s"` (with a window of 30–60 m) in `orchestra.toml`: Orchestra's default
grid is 30 s, and a level that changes inside one step is not a stock.

**Reusing worktrees.** In code mode each ticket gets its own git worktree, made when it is first
worked and removed when it lands. For a repository whose checkout is large, keep a pool instead:

    [tasks]
    reuse_worktrees = true          # off by default
    worktree_pool = 2               # at most this many worktrees, the gate's included (default workers + reviewers + 1)
    worktree_clean_ignored = false  # true: a reset also removes ignored files (build caches, node_modules)

Each turn (a worker's, a reviewer's, a gate run's) gets a free worktree from
`~/.orchestrator/<key>/tasks/pool/`, reset for it: tracked changes discarded, untracked files
removed, ignored files kept unless `worktree_clean_ignored`, and the ticket's branch checked out
(from the base when new). A ticket that comes back gets the worktree it last used when that one
is free. With the pool full a turn waits; the scheduler's log says so, and
`tasks_worktrees{state="waiting"}` counts it. SPEC.md section 5 has the details.

## Writing tickets for the demo's worker

The demo's worker (`demo/machines/shell_worker.py`) runs the ```` ```sh ```` blocks of a ticket's
description, in order, each with `bash -c`, **in the ticket's workspace**: a folder of the
ticket's own under `~/.orchestrator/<key>/tasks/workspaces/<ticket>/`, empty at the first turn
and kept for later turns and for the reviewer. Nothing of the repository is there. So a ticket:

- makes and inspects its own files there (`echo hi > hello.txt`, `cat hello.txt`) and names
  them in the acceptance (`` `hello.txt` ``) for the reviewer to check;
- when it is about the repository, says so: the worker sets `ORCHESTRA_REPO` to the
  repository's path for every command, so `cd "$ORCHESTRA_REPO" && wc -w README.md`. A command
  that assumes the repository silently (`wc -w README.md`, `ls -1`) runs in the empty folder,
  fails, and is rejected every time. RESULT.md's second line says where the commands ran;
- writes the acceptance so a program can check it: a file in backticks exists in the
  workspace, a `"text"` appears in RESULT.md; the reviewer leaves anything else to a person
  and says so;
- runs for at least ten seconds (two of the demo's 5 s discovery steps): real work where there
  is any, otherwise a `sleep` that stands for it, as the demos' first tickets do. A ticket done
  inside one step is stamped IN_PROGRESS and READY_FOR_REVIEW in the same pass and IN_PROGRESS
  never shows as a stock.

A rejected ticket comes back to the worker with the reviewer's feedback. The worker cannot
change commands on feedback, so unless a planner changed the description or the acceptance
first it reports BLOCKED, with the reviewer's first point in the reason and the whole feedback
quoted under it: that is what a planner reads in the banner and acts on. Any other demo that
uses this worker (the agents' demo in orchestra-agents copies it) writes its tickets the same
way.

## Tests

    uv run python -m pytest -q         # about two minutes: the e2e tests drive whole tickets, one suite through a real Orchestra
    uv run ruff check tasking tests demo

The demo suite also executes the Tickets page in a headless Chrome when it finds one: `node` on
the PATH, `puppeteer-core` resolvable by node (`NODE_PATH=<dir>/node_modules`) and Chrome at
`CHROME` (default: the macOS application path). Without them that check is skipped.

## The maintainer's own setup

The `orchestra.toml` at this repository's root and `bin/orchestra-ctl` are the maintainer's
setup for watching this repository with Orchestra itself: the TOML points the bus at Redis
(`redis://127.0.0.1:6379/3`) and the metrics at a VictoriaMetrics on port 8428, and
`orchestra-ctl start` runs Orchestra's host, its web UI on port 8060 and `tasking run` from this
repository's `.venv`, with pids and logs under `run/`. No machine fills the worker and reviewer
roles there, and the capacity is zero (`[tasks] workers = 0`, `reviewers = 0`), so nothing is
offered: it is a watching setup, not a working one. Nothing else in the repository depends on
either file.
