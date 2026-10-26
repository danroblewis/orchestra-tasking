# orchestra-tasking

A task system for AI agents and programs, observed by Orchestra. Tickets and milestones, the loop
that assigns ready tickets to workers and reviewers, the AI agent harnesses (Pi, Claude Code,
Codex), the merge gate and staging.

It was part of Orchestra until 2026-10-25, when Orchestra was reduced to its fundamentals (an
event bus, a metrics store, a system dynamics model). It lives here now, lifted nearly as it was
(`SPEC.md` says what changed), and is to be reshaped into machines that talk over the bus.
Beads was tried in between and removed: its agents choose their own work.

## Running it

    uv sync --extra dev
    bin/orchestra-ctl start            # Orchestra's host and web, http://127.0.0.1:8060
    .venv/bin/tasking run              # the loop: starts the resident AI agents (Supervisor, Coordinator, …)
    .venv/bin/tasking chat             # talk to the Supervisor

Orchestra is installed in this repository's environment from `../orchestra_2` (editable). The
task system adds its pages to Orchestra's web app as a plugin (`tasking/web.py`, the entry point
in `pyproject.toml`): **Tickets** (the ticket graph) and **Agents**.

Its settings are the sections of `orchestra.toml` that Orchestra ignores ([project], [agents],
[gate], …); its state is in `~/.orchestrator/<key>/tasks/`; its events go on Orchestra's bus.

## Tests

    .venv/bin/python -m pytest -q      # about five minutes: the e2e tests drive fake agents through whole tickets
