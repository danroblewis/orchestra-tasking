# orchestra-tasking

A simulated software team working through tickets in **Beads** (`bd`, a real, external issue
tracker), fast enough to produce data, and Orchestra watching from outside.

The team does not know Orchestra exists. It talks only to its tracker, through `bd serve`'s HTTP
API (and the `bd` command for leases). Nothing here writes to Orchestra's event bus or metrics
store. Getting the tracker's events and numbers into Orchestra is the open problem this project
exists to work on.

## The tracker

Beads 1.3.2 (`brew install beads`), on a Dolt SQL server (`bd init --server`), so several workers
can claim work at once: claims are arbitrated in the server. Set up by `bin/tasking-ctl init`:

- the **event journal** is on: every mutation (create, update, close, reopen, claim, dependency,
  comment) is an ordered row, readable at `GET /v0/beads/events?since=N` (seq, ts, op, issue_id,
  actor, a snapshot of the issue). This is what a sync into Orchestra's bus would read;
- a custom status, **in_review** (category wip), for the review step;
- Beads' anonymous usage metrics are **off** (`bd metrics off`);
- `bd init` writes agent hooks (`.claude/`, `.codex/`, `.cursor/`, `.agents/`) that inject Beads
  instructions into coding agents started here; they are removed, since agents Orchestra runs
  here should get their instructions from Orchestra.

States: `open` → `in_progress` (claimed, with a lease kept alive by heartbeats) → `in_review` →
`closed`; a review can send a task back to `open`; QA can reopen a closed task. A task is ready
when it is open and nothing it depends on (blocks) is still open.

## The team (`sim/`, standard-library Python, no Orchestra)

| Role | Instances | Does |
|---|---|---|
| planner | 1 | Opens milestones (epics) at random, about 20 an hour, rising and falling over 45 min; each has 4–9 tasks, each depending on up to two earlier ones; a quarter of milestones depend on the one before. Tasks have a priority, an estimate (2–13 min) and an area label |
| worker | `WORKERS` (4) | Takes the most urgent ready task, works it (its estimate, sped up by `TASKING_SPEED`, times its own skill), heartbeats its lease, hands it to review. 8% of the time files a follow-up task; 2% of the time crashes mid-task and is gone for a while |
| reviewer | `REVIEWERS` (2) | Each takes its share of tasks in review; approves 80% (closed), sends the rest back to open |
| qa | 1 | Looks once at every newly closed task; reopens 6% as escaped defects |
| release | 1 | Closes a milestone when all its tasks are closed; every 30 s reclaims tasks whose lease went stale (a crashed worker's) |

`TASKING_SPEED` (default 12): simulated minutes per real minute.

## Running it

    bin/tasking-ctl init        # a fresh tracker (destroys the issues)
    bin/tasking-ctl start       # Dolt, bd serve (:7811), Orchestra's host and web (:8060), the team
    bin/tasking-ctl status      # processes and the tracker's counts
    bin/tasking-ctl restart worker-2
    bin/tasking-ctl stop
    BD_NON_INTERACTIVE=1 bd list --status in_review     # the tracker, directly

Logs are in `run/`. Orchestra's host and web run from `~/orchestra_2` (`ORCHESTRA=` to point
elsewhere) with this repository's `orchestra.toml`; until a collector or sync exists, they see
nothing of the team.
