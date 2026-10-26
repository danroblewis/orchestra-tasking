# orchestra-tasking

Three separate things, the way they would be in a real organisation:

1. **Beads** (`bd`), an ordinary issue tracker, run as a service. Nothing here changes it.
2. **A simulated team** (`sim/`) that uses Beads the way a team would, fast, to make data. It knows
   nothing about Orchestra.
3. **The integrations** (`integrations/`) that bring Beads into Orchestra, written by this
   repository's owners, as they would be for any external system, using Orchestra as a library.

## 1. Beads (`bin/beads-ctl`)

Beads 1.3.2 (`brew install beads`) on a Dolt SQL server (`bd init --server`), with `bd serve`'s HTTP
API on 127.0.0.1:7811. Server mode, because several workers claim work at once and only the server
arbitrates claims (in embedded mode four parallel claims all failed).

`bin/beads-ctl init` makes a fresh tracker and sets it up:

- the **event journal** on: every change to an issue is an ordered record at
  `GET /v0/beads/events?since=N`;
- a custom status **in_review**, for a review step;
- Beads' anonymous usage metrics **off** (`bd metrics off`);
- the agent hooks `bd init` writes (`.claude/`, `.codex/`, `.cursor/`, `.agents/`) removed: they
  inject Beads instructions into coding agents started here.

`bin/beads-ctl start | stop | status`.

## 2. The simulated team (`bin/sim-ctl`)

Standard-library Python, talking to Beads over HTTP (`sim/bd_client.py`; the `bd` command only for
lease heartbeats and reclaiming stale leases). At the default pace, `TASKING_SPEED=120` simulated
minutes a real minute, a task takes 1 to 7 seconds and the team gets through some 60 tasks a minute.

| Role | Instances | Does |
|---|---|---|
| planner | 1 | About 10 milestones (epics) a minute, rising and falling over 10 min; each has 4 to 9 tasks, each depending on up to two earlier ones; a quarter of milestones depend on the one before |
| worker | `WORKERS` (6) | Claims the most urgent ready task, works it, hands it to review; 8% of the time files a follow-up task; 1% of the time crashes mid-task |
| reviewer | `REVIEWERS` (3) | Approves 80% (closed), sends the rest back; sends back one it cannot close because a dependency was reopened |
| qa | 1 | Reopens 6% of newly closed tasks as escaped defects |
| release | 1 | Closes a milestone when all its tasks are closed; reclaims crashed workers' tasks |

States: `open` → `in_progress` → `in_review` → `closed`, with `in_review` → `open` (sent back) and
`closed` → `open` (reopened).

`bin/sim-ctl start | stop | status | restart <role>`. Logs in `run/`.

## 3. Into Orchestra (`bin/orchestra-ctl`)

Orchestra's host and web (port 8060), and two integrations, run with Orchestra's environment
(`~/orchestra_2/.venv`):

- **beads-sync** (`integrations/beads_sync.py`) follows Beads' event journal. Each record becomes:
  - an event `beads.issue.<op>` on Orchestra's bus, from its actor as a machine and instance
    (`worker-3` is `worker/3`; a record with no actor is Beads' own doing, `beads`), with Beads'
    time and the id `beads:<project>:<seq>`;
  - when a status changed, a count on `beads_issue_transitions_total{from, to, type}`, credited to
    the actor's role; a new issue on `beads_issues_created_total{type}`;
  - the number of issues in each status, `beads_issues{status, type}`, from the same records, so
    levels and flows line up exactly.

  It keeps its place in `run/beads-sync.json`. Run again from the start, it publishes only what the
  bus does not hold (found by id) and rewrites the counts at their own times.
- **beads-levels** (`integrations/beads_levels.py`) polls what the journal does not carry: ready
  tasks (`beads_ready`) and blocked issues (`beads_blocked`), every 2 s.

From this alone Orchestra's discovery finds the tracker's workflow: open, in-progress, in-review
and closed tasks, each balanced by its transitions, and the team's roles as machines.

## Running it all

    bin/beads-ctl init && bin/beads-ctl start
    bin/orchestra-ctl start          # web on http://127.0.0.1:8060
    bin/sim-ctl start
    ~/orchestra_2/.venv/bin/orchestra sd discover --strategy conservation --minutes 8 --step-s 5
