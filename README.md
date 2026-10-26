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

Four tickets are created and worked while you watch: two are done, two end blocked for a
planner to decide. `demo/README.md` says what you will see and where. Ctrl-C stops everything.

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
