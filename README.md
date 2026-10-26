# orchestra-tasking

A task system observed by Orchestra. Tickets and milestones, a scheduler that offers ready
tickets to the `worker` and `reviewer` roles over Orchestra's event bus and applies what the
machines that take them report, a merge gate for code, and the Tickets page and Tasks panel
inside Orchestra's web UI (an Orchestra plugin, `tasks`).

It runs no machine of its own. Whatever fills a role is another program that speaks the
contract in `tasking/assignments.py`: the demo's are a shell runner and a file checker, each a
page of Python using only Orchestra's library (`demo/machines/`). `SPEC.md` is the design;
`AGENTS.md` the rules.

## Try it

    uv sync --extra dev
    uv run demo/run.py                 # a fresh demo project at ~/orchestra-tasking-demo, http://127.0.0.1:8071

Four tickets are created and worked by the two programs; in about fifteen seconds two are done
and two are blocked for a planner to decide. `demo/README.md` says what you will see and where.
Ctrl-C stops everything.

## Use it on a repository

    cd <repo> && uv run --project <this repo> tasking init     # the task system's sections in orchestra.toml
    uv run orchestra web                                        # Orchestra's UI with the Tickets page
    uv run tasking run                                          # the scheduler
    uv run tasking api --as planner create_milestone --args '{"name": "First"}'
    uv run tasking api --as planner create_ticket --args '{"title": "…", "milestone_id": "M1", "description": "…", "acceptance": "…"}'

Then run something that fills the roles (`demo/machines/` as they are, or your own). Settings
are the `[project]`, `[tasks]`, `[gate]`, … sections of `orchestra.toml`; state is in
`~/.orchestrator/<key>/tasks/`; events go on Orchestra's bus.

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
roles there, so tickets created in it are offered and offered again; it is a watching setup, not
a working one. Nothing else in the repository depends on either file.
