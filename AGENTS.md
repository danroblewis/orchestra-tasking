# Rules for working on the task system

Read `SPEC.md` first. It is the design. This file is the short list of rules that must not be
broken without updating the spec and saying so in the commit.

## Invariants

1. **Only the scheduler changes a ticket's state**, from a machine's report or a planner's
   request on the bus, by the transition table in `tasking/model.py`. Never add a path where
   anything else moves a ticket, or where a report's words are translated into another state.
2. **No zombies.** A ticket is `IN_PROGRESS`, `IN_REVIEW` or `MERGING` only while an assignment
   with a live lease holds it. A lease that runs out, a report the task system cannot apply, the
   operator's stop: each releases the assignment and sends the ticket back to waiting in the same
   code path; on start and on shutdown, every holder is told to stop and every active ticket
   goes back.
3. **The task system starts no machine and knows none.** It offers work to a role and reads an
   acceptance's `instance` and a report's `report`; nothing else about the machine. No session,
   transcript, prompt, harness or model is named anywhere in this repository. A machine from
   another package is known only by what it publishes; this package imports nothing from it.
4. **Every crossing is on the bus.** Offers, acceptances, heartbeats, reports, releases,
   requests, answers and attention are CloudEvents through `tasking.events.Events` (over
   `orchestra.events` and `orchestra.bus`), with the ticket as subject and the machine as source.
   The contract is the table in `tasking/assignments.py` and the names in `tasking/requests.py`;
   another package implements the same names, so change them deliberately and say so.
5. **Merging is programmatic.** No machine merges, pushes, touches the base branch or starts
   staging. Only the gate lands code; only a landing deploys staging.
6. **State lives in `~/.orchestrator/<key>/tasks/`**, except the events (Orchestra's bus) and the
   metrics (Orchestra's metrics store). Only `tasking/store/` reads or writes the JSON files, under
   the file lock. Nothing else touches a backend directly.
7. **Humans are read-only, except the operator controls.** The pages and CLI read; the only
   writes a person has are pausing pickup, the capacity ceiling and stopping whoever holds a
   ticket. A person who wants a ticket created or changed does it through a planner machine's
   request (`tasking api` is one), never through a UI form.
8. **Every ticket has a milestone, and closed milestones are final.** The store enforces it.
9. **Orchestra is a library, and the only one the machines need.** `demo/machines/` import only
   `orchestra.config` and `orchestra.events`; they are the example of how to fill a role and must
   stay about a page each, readable, with no words from any other package.
10. **One package, no path hacks.** Everything importable is under `tasking/`; `demo/` is a
    template project and its runner, not a package. No `sys.path` manipulation; the one
    exception is pytest's `pythonpath` in `pyproject.toml`, for the tests only, so that they can
    import `conftest`, `fake_machine` and `demo.run`. Orchestra comes from `../orchestra_2`
    (editable) and the task system uses its bus, metrics store, config and plugin interface,
    nothing else.

## Conventions

- Python 3.12+, `uv` for everything, `pytest` (`uv run python -m pytest -q`), `ruff` for lint
  (`uv run ruff check tasking tests demo`, never `ruff format`; the rule choices and their
  reasons are in `pyproject.toml`).
- `tests/unit` is fast and in-process. `tests/e2e` drives the real scheduler, store, git and
  gate: in-process with `tests/fake_machine.py` (extend the fake rather than mocking the
  scheduler), and as processes through a real `orchestra run` and `orchestra web` in
  `test_demo_e2e.py`, which uses `demo/run.py`'s own functions. The fixtures they share are in
  `tests/conftest.py`. Run the tests before every commit.
- Keep modules small and single-purpose. If a file needs a section divider comment, it should
  be two files. Comments and docs in plain language, honest about limits.
