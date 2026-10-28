# orchestra-tasking

A task system observed by Orchestra. Tickets and milestones, a scheduler that offers ready
tickets to the `worker` and `reviewer` roles over Orchestra's event bus and applies what the
machines that take them report, a merge gate for code, and the Tickets page and Tasks panel
inside Orchestra's web UI (an Orchestra plugin, `tasks`).

How it works, with diagrams and the full contract for workers and reviewers: [ARCHITECTURE.md](ARCHITECTURE.md).

It runs no machine of its own besides the merge gate (code mode); its scheduler is no machine
either, only the daemon that does each role's scheduling and publishes as the machine it acts for
(SPEC.md section 3, "Who publishes what"). Whatever fills a role is another program that speaks the
contract in `tasking/assignments.py`: the demo's are a shell runner and a file checker, each a
page of Python using only Orchestra's library (`tasking/machines/shell_worker.py`, shipped in
the package and run as `python -m tasking.machines.shell_worker` and `python -m tasking.machines.check_reviewer`). `SPEC.md` is the design;
`AGENTS.md` the rules.

## Try it

    uv sync --extra dev                # Orchestra comes from GitHub, at the commit uv.lock pins
    uv run demo/run.py                 # a fresh demo project at ~/orchestra-tasking-demo, http://127.0.0.1:8071

Four tickets are created and worked by the two programs; in about half a minute two are done
and two are blocked for a planner to decide. `demo/README.md` says what you will see and where.
Ctrl-C stops everything; remove `~/orchestra-tasking-demo` first to run it again.

## Use it on a repository

Install Orchestra with the task system beside it, once (no clone needed):

    uv tool install 'orchestra[sd] @ git+https://github.com/danroblewis/orchestra@main' --with 'orchestra-tasking @ git+https://github.com/danroblewis/orchestra-tasking'

The task system's command line comes with it as `orchestra tasks …` (there is no `tasking`
executable). Then, in the repository:

    cd <repo>
    orchestra tasks init        # Orchestra's orchestra.toml (if there is none) with the task system's sections
    orchestra web               # Orchestra's UI with the Tickets page; open the login link it prints
    orchestra run               # Orchestra's host; it starts the task system's scheduler too
    orchestra tasks api --as planner create_milestone --args '{"name": "First"}'
    orchestra tasks api --as planner create_ticket --args '{"title": "…", "milestone_id": "M1", "description": "…", "acceptance": "…"}'
    orchestra tasks status      # one screen: what needs a decision, who holds what, the queue

There is nothing else to start: `orchestra run` keeps the scheduler running (`tasks.scheduler` on
the Core page). **But the task system does no work itself.** It offers ready tickets to the
`worker` and `reviewer` roles, and until something fills them, every ticket waits in `OPEN`.
Fill them one of two ways.

**Programs** (the package's own two, enough for shell tasks): declare them in `orchestra.toml`
so that `orchestra run` keeps them running (`python` there is Orchestra's own, which has the task
system installed):

    [[machines]]
    name = "worker"
    command = "python -m tasking.machines.shell_worker"
    [[machines]]
    name = "reviewer"
    command = "python -m tasking.machines.check_reviewer"     # code mode: add --check '<command>' to check something else than the gate's unit command

**AI agents**: install the agents plugin into the same tool, which fills the roles with agents
(its README says how to configure them):

    uv tool install 'orchestra[sd] @ git+https://github.com/danroblewis/orchestra@main' --with 'orchestra-tasking @ git+https://github.com/danroblewis/orchestra-tasking' --with 'orchestra-agents @ git+https://github.com/danroblewis/orchestra-agents'

Installing the tool again replaces it, so name every plugin you want on each install. Or run your
own machines, anything that speaks the contract in `tasking/assignments.py` over the bus.

The package's reviewer, in tasks mode, checks the acceptance text's `` `files` `` and `"lines"` against the
ticket's folder and RESULT.md; in code mode it runs `--check`, or the first `[[gate.unit]]`
command, in the ticket's worktree at the commit to review (`ORCHESTRA_REPO` names the worktree
there, as it does in the gate) and approves when it passes. Settings are the `[project]`, `[tasks]`, `[gate]`, … sections of `orchestra.toml`; state is in
`~/.orchestrator/<key>/tasks/`; events go on Orchestra's bus. If your tasks live seconds, set
`[discovery] step = "5s"` (with a window of 30–60 m) in `orchestra.toml`: Orchestra's default
grid is 30 s, and a level that changes inside one step is not a stock.

**Recurring tickets.** Work that runs on a clock (a morning brief, a weekly update, a monthly
exercise) is a `[[tasks.recurring]]` definition: a schedule on a time zone's wall clock (`every =
"day" | "week" | "month"` at a time, or `cron`) and a ticket template with date placeholders. The
scheduler makes the ticket at each due time, once, as the planner (`planner/recurring`); after
the laptop slept it makes at most one late occurrence, and by default none while the last one is
still open. `orchestra tasks recurring check` says what is wrong with a definition, `orchestra tasks recurring
list` shows the next due times (also on the Tasks panel). The contract is SPEC.md section 7.

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
`tasks_worktrees{state="waiting"}` counts it (read by the instrument `tasks.tickets`, which
`orchestra run` runs every discovery step: `orchestra tasks metrics`). SPEC.md section 5 has the details.

**Flaky tests and test counts.** The gate can run a failed stage again, and counts your tests from
the reports your runner writes:

    [gate]
    rerun_failures = 1                # run a failed spec again up to 1 time (default 0: never)

    [[gate.unit]]
    name = "tests"
    command = "uv run pytest -q --junitxml={reports}/tests.xml"   # {reports}: this run's report directory

    [gate.test_reports]               # optional
    kinds = ["e2e=tests/e2e/*"]       # how to tell kinds apart, before the defaults (e2e, acceptance, unit by path)
    confirm = 3                       # passing runs in a row that confirm a test

A spec that passes on a rerun lands, and the run, the ticket's note and `gate.landed` say it was
flaky. The task system's instrument counts the gate's runs, reruns and flaky stages
(`tasks_gate_runs_total{status}`, `tasks_gate_reruns_total`, `tasks_gate_flaky_stages_total`), and
in code mode a second instrument, `tasks.tests` (Orchestra's test-report instrument over the gate's
reports), counts the tests by kind and outcome, the flaky and confirmed ones, and the tests added
and removed, so growth in tests is a stock in the model and a regression a rise in failures. The
gate's own stages are there too, as tests of the kind `gate` (`gate.xml`, one per run). A test is
flaky when it both failed and passed within the last 10 runs (or passed on a rerun), confirmed
when it passed its last 3 runs. SPEC.md sections 5 and 8.

**External review (pull requests).** Where code reaches the base branch through your company's
pull requests, the gate can hand an approved branch off instead of landing it, and the ticket
follows the pull request until it is merged:

    [project]
    work = "code"

    [gate]
    mode = "external"                 # land (the default) | external

    [gate.external]
    repository = "acme/service"       # only this repository's pull requests
    failing_labels = ["SAIL-failed"]  # re-engage the worker that wrote the change
    passing_labels = ["SAIL-passed"]  # noted; only a merge closes the ticket
    line_comments = true              # review comments on lines re-engage it
    conversation_comments = false
    failing_checks = true             # a failing check re-engages it
    settle = "15s"                    # gather a review's comments first, then re-engage once

An approved ticket goes to `IN_EXTERNAL_REVIEW`, and `gate.handed_off` names its branch and commit.
You push the branch (under the ticket's branch name, `orchestra/<ticket>-<slug>`) and open the pull
request yourself: the task system never pushes or opens one. orchestra-bridges' pull-request
bridge (`[bridges.github_prs]`) publishes what happens on it. Changes requested, line comments, a
failing check or a failing label send the work back to the worker instance that did it (it resumes
its own work on the same branch), with the feedback as metadata: who, how many comments, which
files and lines, which label or check; the texts stay on the pull request. Its next approval hands
the new commit off again, for you to push. Feedback about an older commit is noted, not acted on.
The merge makes the ticket `DONE`; a close without a merge blocks it for a planner; a reopen
resumes it. SPEC.md section 5 has the details.

## Writing tickets for the demo's worker

The demo's worker (`tasking/machines/shell_worker.py`) runs the ```` ```sh ```` blocks of a ticket's
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
way. A copy needs no change for the scheduler no longer being a machine (2026-10-27): nothing in
the worker reads an offer's or a release's source, and who holds a turn is the bus's order.

## Tests

    uv run python -m pytest -q         # about two minutes: the e2e tests drive whole tickets, one suite through a real Orchestra
    uv run ruff check tasking tests demo

The demo suite also executes the Tickets page in a headless Chrome when it finds one: `node` on
the PATH, `puppeteer-core` resolvable by node (`NODE_PATH=<dir>/node_modules`) and Chrome at
`CHROME` (default: the macOS application path). Without them that check is skipped.

## Developing against a local Orchestra

`pyproject.toml` takes Orchestra from GitHub (`orchestra @ git+https://github.com/danroblewis/orchestra@main`),
and `uv.lock` pins the commit it resolved to, so nothing needs cloning beside this repository.
`uv lock --upgrade-package orchestra` moves the pin to the newest `main` (then `uv sync --extra dev`).

To run against an Orchestra checkout instead, with its uncommitted changes, lay it over this
environment for one command:

    uv run --with-editable ~/orchestra_2 python -m pytest -q      # the path to your Orchestra checkout
    uv run --with-editable ~/orchestra_2 orchestra web

Nothing is written: `.venv`, `pyproject.toml` and `uv.lock` keep GitHub's Orchestra, and a plain
`uv run` uses it again. Inside the command, `sys.executable` and the `orchestra` beside it import
the checkout, so the processes the tests and the host start do too.

For something that runs `.venv/bin/orchestra` directly, install the checkout into `.venv` and keep
uv from syncing it away:

    uv pip install -e ~/orchestra_2
    uv run --no-sync python -m pytest -q          # or export UV_NO_SYNC=1
    uv sync --extra dev                            # back to the locked Orchestra from GitHub

A plain `uv run` or `uv sync` also puts the locked Orchestra back.

## The maintainer's own setup

The `orchestra.toml` at this repository's root and `bin/orchestra-ctl` are the maintainer's
setup for watching this repository with Orchestra itself: the TOML points the bus at Redis
(`redis://127.0.0.1:6379/3`) and the metrics at a VictoriaMetrics on port 8428, and
`orchestra-ctl start` runs Orchestra's host, its web UI on port 8060 and `orchestra tasks run` from this
repository's `.venv`, with pids and logs under `run/`. No machine fills the worker and reviewer
roles there, and the capacity is zero (`[tasks] workers = 0`, `reviewers = 0`), so nothing is
offered: it is a watching setup, not a working one. Nothing else in the repository depends on
either file.
