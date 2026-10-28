# Rules for working on the task system

Read `SPEC.md` first. It is the design. This file is the short list of rules that must not be
broken without updating the spec and saying so in the commit.

## Invariants

1. **The task system is the record, not the hub** (the owner's decision D17, 2026-10-27). Work
   passes directly between the machines that do it (a review requested, changes requested of the
   worker that did the work, an approval for the gate); the scheduler records each step, by the
   transition table in `tasking/model.py`, and is the only writer of the record. Never add a path
   where the scheduler relays between machines, where anything else writes the record, or where a
   machine's words are translated into another state. **The one exception is external review**
   (`[gate] mode = "external"`, SPEC section 5, 2026-10-27): the people and checks on a pull
   request speak no protocol, so what the pull-request bridge publishes of them (`github.pr.*`) moves
   the ticket, and feedback there is offered back to the worker as `changes.requested` published
   as whoever gave it, by `tasking/external.py` alone, with the bridge's event as its cause.
   **The scheduler is no machine** (the owner's decision of 2026-10-27, D17 refined). It is one
   daemon standing in for each machine's own scheduling: the coordinator's dispatch, the leases
   of the worker's and the reviewer's turns, the merge gate, the answers to a planner's requests. It never announces itself and never
   publishes as `task_scheduler` (`tasking/events.py` refuses it, and the store refuses a move it
   records without naming the machine it is for). Every event it publishes goes out as the
   machine whose responsibility it implements at that moment, and only for an act that machine's
   own process did not already announce: offers, and withdrawals of offers nobody claimed, as
   `coordinator/dispatch` (dispatch is the coordinator's), a held turn taken back as its role
   (`worker`, `reviewer`), the gate,
   its pause and staging as `merge_gate`, a request's answer and what it made as the asker,
   attention as the machine whose work is stuck, a recurring ticket made at its due time as
   `planner/recurring` (the planner's scheduled engagement, Orchestra's decision D7; SPEC section
   7) (SPEC section 3, "Who publishes what"). It writes
   no metrics: an instrument the plugin declares reads them from the record. Never add an event
   as the scheduler, a machine declared for it, or a card of its own.
2. **No zombies.** A ticket is `IN_PROGRESS`, `IN_REVIEW` or `MERGING` only while an assignment
   with a live lease holds it, whoever offered it: the scheduler sees every claim. A lease that
   runs out, an end of a turn the task system cannot apply, the operator's stop: each releases the
   assignment and sends the ticket back to waiting in the same code path; on start, every holder is told to stop and every active ticket goes back; on
   shutdown, the gate finishes its run first, then the same.
3. **The task system starts no machine and knows none.** It offers work to a role and reads a
   claim's source and what ended a turn; nothing else about the machine. In `tasking/`,
   `tests/`, `demo/` and SPEC.md's description of the system, no session, transcript, prompt,
   harness or model is named; the design note in `docs/proposals` records what left and why. A
   machine from another package is known only by what it publishes; this package imports nothing
   from it.
4. **Every crossing is on the bus.** Offers, claims, heartbeats, hand-offs, reports, releases,
   requests, answers and attention are CloudEvents through `tasking.events.Events` (over
   `orchestra.events` and `orchestra.bus`), with the machine as source and the ticket as subject
   of every act on it; a query and its answer carry none. **No echoes**: the record is the store,
   and nothing restates a step another machine announced (there is no `ticket.transitioned`,
   `ticket.note_added`, `ticket.updated`, `milestone.updated`, `milestone.closed` or
   `scheduler.stopped`); a ticket's state and notes are read from the record and its moves are
   measured.
   The contract is the table in `tasking/assignments.py`, the names in `tasking/requests.py` and
   the observation families in SPEC section 3; another package implements the same names, so
   change them deliberately and say so.
5. **Merging is programmatic.** No machine merges, pushes, touches the base branch or starts
   staging. Only the gate lands code; only a landing deploys staging. With external review the
   gate lands nothing: it hands the branch off, the owner pushes it and opens the pull request, and
   the merge happens on the pull request; the task system never pushes or opens one. With stacked
   branches (`[gate] stacking`) only the gate restacks a ticket's branch, and fetching a squash
   commit moves no branch.
6. **State lives in `~/.orchestrator/<key>/tasks/`**, except the events (Orchestra's bus) and the
   metrics (Orchestra's metrics store, filled by the instrument `tasks.tickets` from the counters
   the record keeps, and in code mode `tasks.tests` from the gate's test reports). Only `tasking/store/` reads or writes the JSON files, under
   the file lock. Nothing else touches a backend directly.
7. **Humans are read-only, except the operator controls.** The pages and CLI read; the only
   writes a person has are pausing pickup, the capacity ceiling and stopping whoever holds a
   ticket. A person who wants a ticket created or changed does it through a planner machine's
   request (`orchestra tasks api` is one), never through a UI form.
8. **Every ticket has a milestone, and closed milestones are final.** The store enforces it.
9. **Orchestra is a library, and the only one the machines need.** `tasking/machines/` (the
   shell worker and the check reviewer, shipped so that projects install them rather than copy them)
   import only `orchestra.config` and `orchestra.events`, and nothing in the task system imports
   or starts them (`tests/unit/test_machines_stand_alone.py`); they are the example of how to fill a role and must
   stay a few pages each, readable, with no words from any other package.
10. **One package, no path hacks.** Everything importable is under `tasking/`; `demo/` is a
    template project and its runner, not a package. No `sys.path` manipulation; the one
    exception is pytest's `pythonpath` in `pyproject.toml`, for the tests only, so that they can
    import `conftest`, `fake_machine` and `demo.run`. Orchestra comes from GitHub
    (`orchestra @ git+https://github.com/danroblewis/orchestra@main`, the commit pinned in
    `uv.lock`; README, "Developing against a local Orchestra") and the task system uses its bus, config and
    plugin interface (whose instrument fills the metrics store; the task system writes no metric
    itself), nothing else. Configuration is read and changed only through Orchestra's objects
    (`project.orchestra.config.raw`, `reload_config()`, `config_edit`), never by opening
    `orchestra.toml` (Orchestra's rule 10), so the owner's private overlay reaches the task
    system; `tests/unit/test_config_via_orchestra.py` fails otherwise.

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
