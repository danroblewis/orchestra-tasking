> **Lifted, not yet reshaped.** This is Orchestra's specification as it was just before the task
> system was removed from it (Orchestra commit `5006caf^`, 2026-10-25). It describes the code now
> in `tasking/` with these differences: the package is `tasking`, the command is `tasking`
> (`orchestra web`, the metrics, the model and the event log stay Orchestra's); the pages are an
> Orchestra plugin (Tickets at `/tickets`, Agents at `/agents`, API at `/api/plugins/tasks`);
> events are CloudEvents on Orchestra's bus (source `tasks` for the loop, `<role>/<ticket>` for an
> agent's tool call); state is in `~/.orchestrator/<key>/tasks/`. The ticket factory model,
> the old Dashboard and the old Machines panel are gone (Orchestra discovers the model and observes
> the machines). The spec is to be rewritten as the system is reshaped into machines.

# Orchestra Specification

Orchestra is a ticket tracker, milestone planner, and CI/CD workflow in one tool,
operated by AI agents running on the Pi harness. A human describes work to a
Supervisor agent; Orchestra turns that into tickets, has worker agents build
each ticket on its own git branch, has reviewer agents check the work, and
lands approved branches on `main` through a batched, bisecting merge gate.

It is a `uv`-installable Python package that can be pointed at any git
repository. Its own state lives in `~/.orchestrator/`, grouped by the
repository's working directory, behind a storage interface so tickets can
later live in GitHub Issues, Jira, or Linear instead of a local file.

This document is the source of truth for the design. `AGENTS.md` lists the
rules that must not be broken; this file explains everything else.

---

## 1. Principles

1. **Agents judge, Orchestra decides.** Every state change to a ticket is made
   by Orchestra's programmatic loop. Agents report in their own vocabulary and
   Orchestra applies the transition table. An agent saying it is done never
   makes a ticket done.
2. **No zombies.** A ticket is in an active state only while a live agent or
   process owns it. When that stops, for any reason, the ticket moves to a
   waiting state immediately.
3. **The agent's word is the state.** Workers say `READY_FOR_REVIEW` or
   `BLOCKED`. Reviewers say `APPROVED` or `REJECTED`. Orchestra does not
   translate one word into another.
4. **One worktree per ticket, one session per agent run.** Workers never
   touch `main` or each other's files. Sessions are files on disk and resume
   from where they stopped.
5. **Merging is programmatic.** No agent merges. The merge gate builds the
   project, tests the built artifact, lands a batch of approved branches,
   bisects on failure, and promotes the artifact to staging on success.
6. **State is outside the repository.** The target repository gets a
   config file and nothing else. Worktrees, sessions, runs, and the local
   ticket store live under `~/.orchestrator/`.
7. **Storage is an interface.** Business logic talks to a `TicketBackend`.
   The local JSON backend is the first implementation, not the design.

---

## 2. Domain model

### 2.1 Project

A project is one git repository Orchestra manages.

| Field | Meaning |
|---|---|
| `key` | `<dirname>-<8 hex of sha256(realpath)>`. Stable, unique, readable. |
| `repo_root` | Absolute real path of the repository. |
| `base_branch` | Branch the gate lands on. Default `main`. |
| `ticket_prefix` | Prefix for ticket IDs, e.g. `ORC` gives `ORC-12`. |
| `config` | Parsed `orchestra.toml` from the repository root. |

### 2.2 Ticket

The unit of work. Called a ticket everywhere; never "task". Every ticket
belongs to exactly one milestone.

| Field | Meaning |
|---|---|
| `id` | Ticket ID: `<PREFIX>-<n>`, sequential per project in the local backend. External backends supply their own. |
| `title`, `description` | What to build. Description is the worker's brief and must be self-contained. |
| `acceptance` | Acceptance criteria, free text. Given to worker and reviewer. |
| `objective` | Why: which project objective this ticket serves. Set by the Supervisor; the Coordinator may not change it. |
| `verification` | How the merge gate proves it: the acceptance test(s) that cover it, or the test the work must add. Part of the work for the worker, checked by the reviewer. |
| `state` | One of the states in section 3. |
| `priority` | Integer 1 to 9, lower is sooner; 5 by default. The loop orders pickup by **effective priority**: a ticket inherits the best priority of anything that transitively depends on it, so prioritizing an outcome pulls its whole prerequisite chain forward, and a ticket gains one level per `aging_days_per_level` days of age so old work is finished or cancelled rather than forgotten. Ties broken by creation order. |
| `milestone_id` | Required. Must refer to an open milestone at creation and whenever changed. |
| `depends_on` | Ticket IDs that must be `DONE` before this one is ready. |
| `branch` | `orchestra/<id>-<slug>`. Set when first picked. |
| `worker_session`, `reviewer_session` | Pi session ids. Set when first started, kept for resumption. |
| `reviewed_commit` | Commit hash the current `APPROVED` applies to. |
| `blocked_reason` | Required when `BLOCKED`. |
| `feedback` | Reviewer or gate output handed back to the worker when `REJECTED`. |
| `rejected_by` | `reviewer` or `merge_gate`. |
| `review_rounds` | Count of `REJECTED` transitions. |
| `created_by` | `supervisor`, `coordinator`, or `pipeline_manager`. |
| `version` | Optimistic concurrency counter. Every write checks it. |
| `created_at`, `updated_at` | Timestamps. |

Tickets also carry an append-only list of **notes**: timestamped entries from
agents. Reviews, gate results, and blocker explanations are notes.

Tickets are created and edited only by agents through their tools. There is
no human editing path; a human who wants a ticket changed tells the
Supervisor.

### 2.3 Milestone

A milestone groups and organizes tickets. It is not a sprint and has no
schedule semantics; the loop works any ticket in any open milestone.

| Field | Meaning |
|---|---|
| `id` | Milestone ID: `M<n>`, sequential per project. |
| `name`, `description` | What the group is for. |
| `order` | Display and planning order. |
| `status` | `open` or `closed`. |
| `created_at`, `closed_at` | Timestamps. |

Rules, enforced by the store so no backend or agent can bypass them:

- A ticket can only be created in, or moved to, an open milestone.
- A milestone can be closed only when every ticket in it is `DONE` or
  `CANCELLED`.
- A closed milestone can never be reopened and can never receive tickets.

### 2.4 Local-only records

These never go to an external ticket backend. They are facts about this
machine's execution and live in the project's state directory.

- **Session**: `id`, `kind` (supervisor, coordinator, pipeline_manager,
  worker, reviewer), `ticket_id`, `pi_session_file`, `status` (`running`,
  `idle`), `started_at`, `settled_at`, `last_text`.
- **Worktree**: `ticket_id`, `path`, `branch`, `created_at`.
- **GateRun**: `id`, `batch` (ticket IDs), `base_commit`, `gate_commit`,
  `status`, `build_id`, `results` per stage and spec, `bisection` record,
  `merged`, `rejected`, `started_at`, `finished_at`, `log_path`.
- **Build**: `id`, `gate_run_id`, `commit`, `artifact_path`, `created_at`.
  The artifact file itself lives at `<state>/artifacts/<build-id>/`.
- **Staging**: `current_build_id`, `previous_build_id`, `pid`, `port`,
  `local_url`, `public_url`, `status` (`running`, `starting`, `failed`,
  `stopped`), `deployed_at`, `last_health_at`.
- **Event**: append-only log of everything that happened, used by the UI, by
  agent wake-ups, and for debugging. See section 6.5.

---

## 3. Ticket states

Ten states in three groups. Readiness is derived, not stored: a ticket is
ready when it is `OPEN`, every `depends_on` ticket is `DONE`, and its
milestone is open.

### Waiting states (no agent owns the ticket)

| State | Waiting for | Set by |
|---|---|---|
| `OPEN` | A worker slot. | Creation; crash recovery; Coordinator/Supervisor unblocking. |
| `BLOCKED` | The Supervisor (relaying the human) or the Coordinator. `blocked_reason` required. | Worker's word; Orchestra when a worker stops without reporting. |
| `READY_FOR_REVIEW` | A reviewer slot. Pins the commit to review. | Worker's word. |
| `REJECTED` | A worker slot, to address `feedback`. | Reviewer's word; the merge gate. |
| `APPROVED` | The next merge batch. Pins `reviewed_commit`. | Reviewer's word. |

### Active states (exactly one live owner)

| State | Owner |
|---|---|
| `IN_PROGRESS` | A running worker session. |
| `IN_REVIEW` | A running reviewer session. |
| `MERGING` | A running gate on a batch containing this branch. |

### Terminal states

| State | Meaning |
|---|---|
| `DONE` | The branch is on `base_branch`. Worktree removed. |
| `CANCELLED` | Abandoned via the Supervisor. Worktree removed, branch kept. |

### Transitions

| From | To | Cause |
|---|---|---|
| `OPEN` | `IN_PROGRESS` | Loop: ready, slot free, worktree created or reclaimed, worker started or resumed. |
| `IN_PROGRESS` | `READY_FOR_REVIEW` | Worker reports it. Orchestra records the branch head as the commit to review. |
| `IN_PROGRESS` | `BLOCKED` | Worker reports it with a reason; or worker stopped without reporting (Orchestra sets the reason). |
| `READY_FOR_REVIEW` | `IN_REVIEW` | Loop: reviewer slot free, tests run in the worktree, reviewer started. |
| `IN_REVIEW` | `APPROVED` | Reviewer reports it. `reviewed_commit` set. |
| `IN_REVIEW` | `REJECTED` | Reviewer reports it with feedback; or reviewer stopped without a verdict. |
| `REJECTED` | `IN_PROGRESS` | Loop: worker slot free, feedback delivered to the worker's session. `review_rounds` incremented. |
| `APPROVED` | `MERGING` | Loop: picked into a gate batch. |
| `MERGING` | `DONE` | Gate landed it. |
| `MERGING` | `REJECTED` | Gate attributed a failure to it. `rejected_by = merge_gate`. |
| `MERGING` | `APPROVED` | Gate was aborted or `main` was found broken; nothing attributed. |
| `BLOCKED` | `OPEN` | Supervisor or Coordinator resolved it. |
| any non-terminal | `CANCELLED` | Supervisor. |
| `IN_PROGRESS` | `OPEN` | Crash recovery only. |
| `IN_REVIEW` | `READY_FOR_REVIEW` | Crash recovery only. |

Rules:

- A ticket returning to `IN_PROGRESS` must pass through `READY_FOR_REVIEW`
  and `IN_REVIEW` again. Approvals are pinned to a commit; the gate refuses
  to merge any other commit.
- After `review_rounds` reaches the configured threshold, the Coordinator is
  woken to decide: continue, split, block, or cancel. Nothing fails
  automatically.
- If a dependency is `CANCELLED`, dependents stay `OPEN` and never become
  ready until the Coordinator edits them.
- On startup, Orchestra moves every ticket in an active state to the waiting
  state before it. Sessions and branches are untouched, so the next pickup
  resumes rather than restarts.

---

## 4. Agents

Five session kinds. Each is a Pi session, which means a process that acts only
when prompted and a JSONL file that persists between prompts.

### 4.1 Lifetimes and communication

| Kind | Sessions | Lifetime | Woken by |
|---|---|---|---|
| Supervisor | one per project | project | The human, in `orchestra chat`. |
| Coordinator | one per project | project | Orchestra events: ticket created, `DONE`, `BLOCKED`, review threshold reached, milestone created or closed. |
| Pipeline Manager | one per project | project | Orchestra events: base branch failing with no branches applied, gate run over the time budget, repeated merge rejections of one ticket, staging deploy failure. |
| Worker | one per ticket | ticket | The loop, each time the ticket enters `IN_PROGRESS`. |
| Reviewer | one per ticket | ticket | The loop, each time the ticket enters `IN_REVIEW`. |

No agent talks to another agent. Orchestra reads an agent's output, decides
what it implies, and prompts the next agent. Who may cause a message to reach
whom:

| From | To |
|---|---|
| Human | Supervisor only. There is no other way for a human to change a ticket or milestone. |
| Supervisor | Coordinator, Pipeline Manager, via Orchestra events. |
| Coordinator | Pipeline Manager, Workers, via tickets and notes. |
| Pipeline Manager | Coordinator, Workers, via tickets and notes. |
| Worker, Reviewer | Nobody. They report. |

Three Pi mechanisms deliver a message: `prompt` when idle, `steer` to
interrupt a running agent after its current tool call, `follow_up` to queue
for after the run. Orchestra waits for the `agent_settled` event before
reading a result.

### 4.2 Responsibilities

**Supervisor.** Converses with the human. Its job is translation, not
transcription: it places each request against the project's objectives,
resolves material ambiguity by asking or by stating assumptions, defines the
outcome and how the gate will verify it, and only then creates tickets, each
with an `objective` and a `verification`. It maintains the **project context
document** (`<state>/PROJECT.md`, seeded from `.orchestra/PROJECT.md` in the
repository when present): objectives, value propositions, non-goals,
definition of done, verification strategy. Every Supervisor turn begins with
that document. Never writes code, never spawns anything. Woken by the human
and by escalations from other agents.

**Coordinator.** Keeps the plan executable: priorities, dependencies,
milestone membership, splitting tickets that are too big, writing tickets
that are clear enough for a worker to act on without asking. Decides what to
do with a ticket that keeps getting rejected. Never changes a ticket's intent:
it may rephrase, split, order, and reprioritize within the `objective`, and
when it thinks the intent is wrong it calls `escalate_to_supervisor` instead
of rewriting. Never spawns a worker; the loop does that, so the Coordinator's
latency never gates execution.

**Pipeline Manager.** The on-call engineer for the merge pipeline and
staging. Reads gate and staging logs. Creates fix tickets when `main` is
broken, a build fails on `main`, staging will not come up, or a test is
flaky. Quarantines a test spec when told to by configuration policy. Never
merges, never deploys by hand; it can ask for a staging restart through
`restart_staging`.

**Worker.** Implements one ticket in its worktree on its branch. Reads the
repository's conventions. Commits as it goes. Runs the project's tests
before reporting. Reports exactly one of `READY_FOR_REVIEW` or `BLOCKED`.

**Reviewer.** Reviews one commit the way a pull request reviewer does: reads
the diff and the changeset, checks that the code is correct, complete
against the acceptance criteria, consistent with the repository's
conventions, and does not do more or less than the ticket asks. It runs
inside the same worktree so it can open any file for context. Orchestra has
already run the unit tests and hands it the output; the reviewer may run
tests or other checks itself, but that is not its job. Its job is to read
and comment on the code. Reports exactly one of `APPROVED` or `REJECTED`
with specific, actionable feedback, as line-level comments where possible.

### 4.3 Roles, agents, and teams are data

Three things, kept apart. A **role** is a job description: a name, a description, a
prompt in the second person, and the tool groups it needs. It has no runtime. An
**agent** is an instance of one or more roles placed in a team, and is what runs: it has
a kind (`continuous`, woken by `wake_on` events on a resident session; `scheduled`, woken
`every` interval or `at` a daily time, resident or ephemeral; `ad_hoc`, invoked once with
a brief by its lead through `run_agent`), and it is what gets paused, restarted, messaged,
and shown. A **team** has exactly one lead agent, members, responsibilities (standing
conditions written as markdown, true or false at any moment), access (tool groups and
repository paths, read or write), a capacity, and a space in the repository. Teams nest by
name (`product/qa`); a nested team's lead reports to the parent's lead and its agents see
the parent's responsibilities too.

Built-in roles live in `orchestra/agents/roles.py`; the built-in agents and teams derived
from them (Engineering led by the Coordinator with the ad hoc worker and reviewer, Pipeline
led by the Pipeline Manager, the Supervisor in no team) in `orchestra/org.py`. **Custom
roles, agents, and teams are stored behind store interfaces**, like tickets, whose default
implementations keep them in the target repository: `.orchestra/roles/<name>.json` and
`.md`, `.orchestra/agents/<name>.json`, `.orchestra/teams/<team>.json` and `.md` for the
responsibilities, committed to the base branch when the checkout allows it. A team's
working space is `.orchestra/spaces/<team>/`, created with a `tools/` directory; it is
listed in `.orchestra/.gitignore` unless the team is `tracked`. A Stage 1 role record that
carries wake rules of its own is exposed as an implicit agent of the same name, so nothing
created before teams existed stops running.

An agent's turn is assembled in a fixed order: its roles' prompts, then its team's
responsibilities and those of the teams above it, then its access, then the wake-up or
brief. Its tool groups are the union of its roles' groups and its team chain's grants,
plus `team` when it leads a team. An agent in a team runs with its working directory set
to the team's space and the team chain's `tools/` directories on its `PATH`.

The loop reloads all three every tick: it keeps resident agents' processes alive, starts
new ones, closes deleted or paused ones, tails the event log and queues a wake-up for any
agent whose `wake_on` matches, queues scheduled wake-ups, and wakes ad hoc agents when a
brief is queued for them. Messages follow the tree, enforced in the API: an agent may
message its own lead, a lead its team's agents and its sub-teams' leads, the Supervisor
anyone. Everything else is an escalation.

**Three sources of wake-ups.** Orchestra's own events (tickets, gate runs, staging,
agents) wake any custom agent whose `wake_on` matches; the built-in agents' wake lists are
descriptions of what the loop does for them in code, not subscriptions, and the UI says so.
**Probes** are commands with an interval, from `[[probes]]` in `orchestra.toml` or a team's
`probes` (run in the team's space with its tools on PATH, `STAGING_URL` in the
environment); exit 0 means the condition holds, and `probe_failed:<name>` and
`probe_recovered:<name>` are emitted only on the transition, with the output. **Agent
events** are types a team declares in `emits` and its agents raise with `emit_event`
(a subject and a note); any agent may subscribe. The API refuses undeclared types, more
than twenty emissions per agent per hour, and an emission from a turn whose cause chain
(event wakes agent, which emits, which wakes...) is already three deep. Events are facts
without an addressee; `message_agent` is for one agent, `escalate_to_supervisor` for a
decision.

**The organisation is designed in conversation.** The Supervisor loads `org_guide` when
the human asks for a team, a change to a team, a role, or a scheduled agent, plays the
plan back, and builds it one creation per turn with `create_role`, `create_agent`, and
`create_team`, preferring `update_team` on an existing team over a new one. The Agents
page shows teams with their agents and schedules, agents outside a team, and roles, each
openable to its full definition, with pause, restart, and delete for the human. Jobs,
curated documents, votes, experiments, and team pages are the rest of Stage 2; see
`docs/proposals/teams-and-roles.md`.

### 4.4 Reporting contract

Agents report through a registered tool, not by formatting their final
message. Small models are far more reliable calling a tool with a schema than
emitting exact JSON as prose.

- Workers get `report_ticket(state: READY_FOR_REVIEW | BLOCKED, summary,
  blocked_reason?)`.
- Reviewers get `report_review(verdict: APPROVED | REJECTED, summary,
  feedback?)`.
- Supervisor and Coordinator get the ticket tools: `create_ticket`,
  `update_ticket`, `add_note`, `cancel_ticket`, `unblock_ticket`,
  `create_milestone`, `update_milestone`, `close_milestone`, `list_tickets`,
  `get_ticket`, `list_milestones`, `project_status`. The store enforces the
  milestone rules in section 2.3; a tool call that breaks one fails and the
  agent is told why.
- The Pipeline Manager gets the ticket tools plus `get_gate_run`,
  `get_staging_status`, `restart_staging`, `quarantine_test`,
  `unquarantine_test`.

Tools are provided by one Pi extension that Orchestra ships and loads with
`--extension`. The extension shells out to `orchestra api <command> --json`,
which runs against the project's store with file locking, so tools work
whether or not the main loop is running. The tool result tells the agent
whether the write succeeded and what the ticket now looks like.

Fallback: if an agent settles without calling its report tool, Orchestra
prompts the same session once with "call `report_ticket` now, or report
BLOCKED with the reason". If it settles again without reporting, Orchestra
applies the no-report rule from section 3. As a last resort the final
assistant text is scanned for a JSON object with the same fields.

### 4.5 Prompts

Every session gets two layers: a role prompt via `--append-system-prompt`,
and the specific task as the first `prompt`. Role prompts live in
`orchestra/agents/prompts/*.md` and can be overridden per project from
`orchestra.toml`.

The worker's task prompt includes: ticket ID, title, description,
acceptance criteria, the branch name, the fact that its working directory is
the worktree, the project's test command, and the reporting instructions.
On resumption after `REJECTED`, the prompt is the feedback and the
instruction to address it and report again.

The reviewer's task prompt includes: the ticket, the diff, the test output,
the commit hash under review, and the rule that `APPROVED` means the
acceptance criteria are met and tests pass.

---

## 5. Agent harnesses

Orchestra does not run models; a *harness* does. A harness is the product that turns a
prompt into an agent turn with tools: Pi, Claude Code, Codex, OpenCode. Orchestra talks
to every harness through one small interface (`orchestra/harness/base.py`) and knows
nothing else about it:

- `Harness.start(spec)` opens a named, resumable session with a system prompt, a working
  directory, a model, and a set of Orchestra tool groups.
- `AgentSession.prompt(text, timeout, on_event)` runs one turn to completion and returns
  the final text. Events reach the loop in a **normalized vocabulary** (`turn_start`,
  `assistant_start`, `token`, `tool_start`, `tool_end`, `assistant_end`, `settled`), so
  the UI's "receiving tokens · last token 3s ago" means the same thing on every harness.
- `AgentSession.session_file` is the harness's own transcript, the only one Orchestra
  keeps; `Harness.read_transcript` parses it into a common message list for the UI and
  `orchestra sessions show`.
- `kill()` stops a turn (the operator's stop button); `close()` releases the session.

The project picks its harness in `orchestra.toml` (`[agents] harness = "pi" |
"claude_code"`), with `model` naming the model in that harness's terms and `provider`
used by Pi only.

### 5.1 Orchestra's tools are data

The tools an agent uses to talk to Orchestra (`report_ticket`, `create_ticket`, ...) are
defined once, as JSON-schema records in `orchestra/harness/tools.py`, grouped by tool
group. Each tool maps to an `orchestra api` command. Harnesses expose them in their own
way; none defines its own copy. `orchestra tools --json` prints the catalogue.

### 5.2 Pi

One `pi --mode rpc` process per live session; the process is the session and stays
resident between turns. Launched with `--session-dir <state>/sessions --session-id
<session-id> --provider <p> --model <m> --append-system-prompt <role> --extension
<orchestra/harness/pi_extension.ts>`, with `cwd` set to the worktree for workers and
reviewers and to `repo_root` for the project agents. The extension fetches the tool
catalogue from `orchestra tools --json` at load and registers each tool; calls shell out
to `orchestra api`. One JSON command per line on stdin, events on stdout;
`agent_settled` ends a turn; on timeout Orchestra sends `abort`. Transcript: Pi's own
JSONL session file.

### 5.3 Claude Code

No resident process: each turn is `claude -p` with the prompt on stdin, `--output-format
stream-json --include-partial-messages`, `--append-system-prompt-file <role>`, `--model
<m>`, `--dangerously-skip-permissions`, and `--mcp-config <per-session file>
--strict-mcp-config` pointing at Orchestra's MCP server (`python -m
orchestra.harness.mcp`), which serves the tool catalogue for the session's role and
calls `orchestra.api` in-process. The first turn passes `--session-id <uuid>`; later
turns `--resume <uuid>`. Orchestra keeps the session-id→uuid mapping under
`<state>/sessions/claude-code/`. Transcript: Claude Code's own JSONL under its config
directory (`projects/<cwd>/<uuid>.jsonl`). A session is "alive" while the loop holds
it; the loop's pid stands in where the harness keeps no process.

### 5.4 Codex

No resident process: each turn is `codex exec --json` with the prompt on stdin (`-`),
`--sandbox danger-full-access -C <cwd> --skip-git-repo-check`, `-m <model>`, and
`-c` config overrides: `mcp_servers.orchestra={...}` (the same MCP server, declared as a
TOML inline table with the session's `ORCHESTRA_*` env and `required=true`, so a server
that fails to start fails the turn loudly) and `developer_instructions="<role prompt>"`.
Codex assigns the thread id on the first turn (`thread.started`); later turns are
`codex exec ... resume <thread_id> -`. Orchestra keeps the session-id→thread-id mapping
under `<state>/sessions/codex/`. Transcript: Codex's rollout under `$CODEX_HOME`
(default `~/.codex`) at `sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl`. Codex streams
whole items (`item.started` / `item.completed` for `agent_message`, `command_execution`,
`mcp_tool_call`, `file_change`), not tokens, so every item counts as liveness.

Codex presents MCP tools to the model as a Responses API `namespace` tool. OpenAI's API
understands it; an OpenAI-compatible local server (llama-server) does not, so on a local
model Codex agents run but cannot reach Orchestra's tools. Use Codex with OpenAI models
(`codex login` or `CODEX_API_KEY`).

### 5.4a Keeping context bounded

Every harness session is kept from growing without limit, because a long transcript
re-sent on every turn is what makes a shared model server crawl. Before a turn starts
on a session whose transcript has grown past `session_max_bytes` (default 300 KB) since
it was last compacted, the loop asks the harness to compact: Pi summarises older
messages in place through its `compact` RPC command and the agent keeps what it learned;
Claude Code is started with `--autocompact` at `compact_tokens` (default 100,000, the
CLI's floor) and bounds itself; Codex has no compaction control the loop can reach, so a
Codex session that outgrows the limit is rotated: archived as a run and started fresh,
with the wake-up or task carrying the current state. Each case is an `agent_compacted`
or `agent_reset` event shown in the transcript. The human can also reset any resident
agent from the Agents page.

### 5.5 Adding a harness

Implement `Harness` and `AgentSession`, register the class in
`orchestra/harness/__init__.py`, give the e2e suite a fake binary that speaks the
harness's protocol (see `tests/e2e/fake_pi.py`, `fake_claude.py`, and `fake_codex.py`),
and the loop tests run against it unchanged. Harnesses that speak MCP get Orchestra's
tools for free.

Session ids are harness-independent: `supervisor`, `coordinator`, `pipeline-manager`,
`worker-<ticket-id>`, `reviewer-<ticket-id>`, and custom roles by name.

---

## 6. The orchestration loop

`orchestra run` is a single long-running process. It is the only writer of
ticket state while it runs, apart from the `orchestra api` writes made by
agent tools, which are serialized by the store's file lock and
reconciled by version checks.

### 6.1 Slots

Config defines `workers`, `reviewers`, and `gate` (always 1) slot counts.
Each live session occupies one slot in its own thread. The loop ticks every
few seconds and on every event.

### 6.2 Tick

In order, each tick:

1. **Collect.** For each thread that finished: apply the agent's report,
   release the slot, record the session as idle, emit events.
2. **Review.** While a reviewer slot is free and a `READY_FOR_REVIEW` ticket
   exists: run the unit specs in the worktree, build the diff, start the
   reviewer, move to `IN_REVIEW`.
3. **Resume rejected.** While a worker slot is free and a `REJECTED` ticket
   exists (oldest first): deliver feedback to the existing worker session,
   move to `IN_PROGRESS`.
4. **Pick.** While a worker slot is free and a ready `OPEN` ticket exists
   (effective priority, then age): create or reclaim the worktree, start or
   resume the worker, move to `IN_PROGRESS`.
5. **Gate.** If the gate slot is free, the pipeline is not paused, and at
   least one `APPROVED` ticket exists, and either the batch is full or the
   oldest approval is older than `gate.batch_wait`: form a batch and start
   the gate.
6. **Staging.** If staging is configured and its process has died or its
   health probe has failed for longer than the configured tolerance, restart
   it and emit an event; if it fails again, wake the Pipeline Manager.
7. **Wake.** Deliver any pending wake-ups to the Coordinator or Pipeline
   Manager if they are idle, coalescing multiple reasons into one prompt.

Review runs before pick so finished work lands before new work starts.

### 6.3 Worktrees and branches

A worktree exists for exactly the span of a ticket's active life:

- **Created on demand** the first time the ticket enters `IN_PROGRESS`, with
  `git worktree add -b orchestra/<id>-<slug> <path> <base_branch>`.
- **Reused** by every agent that touches the ticket afterwards: the worker
  across `REJECTED` cycles and `BLOCKED`, and the reviewer, which runs in the
  same worktree so it sees exactly what the worker built.
- **Deleted** when the ticket is completed, meaning `DONE` or `CANCELLED`.
  The branch is deleted on `DONE` and kept on `CANCELLED`. The worker and
  reviewer session records are archived with their transcripts, not deleted,
  so a finished ticket's history stays viewable from its drawer.

The path is an implementation detail; the default is
`<state>/worktrees/<ticket-id>`. Before an agent resumes, Orchestra verifies
the worktree exists and is on the ticket's branch, and recreates it from the
branch if it is missing. `orchestra doctor` lists worktrees and branches with
no matching ticket and offers to remove them; the loop never deletes anything
it did not create.

### 6.4 Crash recovery

On startup: kill nothing (previous processes are gone with the previous
run), move active-state tickets to their preceding waiting state, mark all
sessions idle, delete any gate worktree, and verify each ticket's worktree.

### 6.5 Events

Every state change, session start and stop, gate step, and wake-up is an
event: `{ts, type, ticket?, session?, gate_run?, data}`. Events are appended
to the project's **event bus**, published in-process to subscribers (the web
server's WebSocket, the wake-up scheduler), and are the audit trail.

The bus (`orchestra/bus/`) is one ordered, replayable stream per project,
named `orchestra.<key>.events`. Offsets are integers: the number of events
before a point, so `read(0)` starts at the beginning and every read returns
the offset to read from next. Backends, chosen by `[events] url` in
`orchestra.toml` or `ORCHESTRA_EVENTS_URL`:

| URL | Backend |
|---|---|
| `file` (default) | `<state>/events.jsonl`, one event per line |
| `redis://…` | A Redis Stream, ids assigned atomically so offsets stay dense |
| `amqp://…` | A RabbitMQ stream queue (RabbitMQ 3.9+), publisher confirms |
| `kafka://host:port` | One Kafka topic with one partition |

One conformance suite (`tests/bus/`) runs against every backend. Server
backends need their client library (`orchestra[bus-redis]`,
`[bus-rabbitmq]`, `[bus-kafka]`).

### 6.6 Metrics and collectors

Measured series live in a **metrics store** (`orchestra/metrics/`): a
JSONL file in the state directory by default, or VictoriaMetrics
(`[metrics] url = "http://host:port"`, or `ORCHESTRA_METRICS_URL`). Series
are named `orchestra_*` and carry a `project` label. A stock's current
value is a query; a range carries the last value forward at each step.

**Collectors** (`orchestra/collectors.py`) turn the event log into series:

| Series | What it is |
|---|---|
| `orchestra_tickets{state}` | Tickets in each state |
| `orchestra_tickets_created_total` | Tickets created (a cumulative counter) |
| `orchestra_ticket_transitions_total{from,to}` | Tickets that have moved from one state to another (a cumulative counter) |
| `orchestra_events_total{type}` | Events of each type |
| `orchestra_tickets_sampled{state}` | The ticket store's own count, read directly |

Series are named for what they measure, so summing across a label means
something; whether a series is a stock or a flow is the model's business,
decided by what it binds (section 6.7). Only the last value of a series at
an instant is written: events have one-second timestamps, and stores
disagree on the order of points that share one.

Integrated levels are **anchored**: each stock's series is shifted so its
last value equals the sampled count. The shift is reported, as a measure of
what the log missed. `orchestra metrics backfill|series|show` replays the
log, lists series, and prints a stock or flow over time.

### 6.7 The system dynamics model

`orchestra/sd/` holds the model (stocks bound to series, flows bound to
counters, equations, machines, eras), the model store, and inference. There
is **one model** of the system. Every change to it is saved as a new
version (`<state>/model/v0001.json`, …); the highest is current, and past
versions stay readable. Sub-models are views of it, not separate models. Every equation records its **origin**: `fitted` (with method,
window and quality), `discovered`, or `declared` (an assumption until
fitted). A **machine** is a record of something outside Orchestra,
attributed to flows as a hypothesis; it never defines a flow's equation.
Exactly one machine is the **bootstrap** host.

`orchestra sd declare` writes Orchestra's own task system into the model
(ticket states as stocks, transitions as flows, today's loop steps as
machines; descriptive only). `orchestra sd fit` fits every flow from the
metrics store: each step's count is Poisson, with mean `c × running hours`
(constant) or `k × the source stock's item-hours` (proportional, the exact
integral of its level while the loop runs), fitted by maximum likelihood;
the form with the higher likelihood is kept, with its standard error,
dispersion and event count. Hours with the loop stopped are left out. Both
save a new version; `orchestra sd versions` lists them.

---

## 7. The merge gate

The gate answers "are these branches safe on `main` together?" by building
the project, running its unit tests, starting the built artifact, and running
acceptance tests against it. It is entirely programmatic. When it succeeds,
the artifact it built is promoted to the staging environment (section 7.5).

### 7.1 Gate stages

A gate run has four stages, each configured in `orchestra.toml` (section 9):

| Stage | Config | What it does |
|---|---|---|
| build | `[build]` | Produces the artifact: a binary, a wheel, a container image, a directory. The command runs in the gate worktree and must leave the artifact at the configured path. No artifact, no gate. |
| unit | `[[gate.unit]]` specs | Fast tests that need no running system. |
| start | `[acceptance]` | Starts the artifact as the acceptance target on an ephemeral port, waits for the readiness probe. |
| acceptance | `[[gate.acceptance]]` specs | Tests that exercise the running artifact. They receive the target's URL or path through placeholders. The target is stopped afterwards. |

Unit specs may define `select`, a template for rerunning named failures.
Acceptance specs always rerun in full, since they depend on a fresh start of
the target.

Acceptance tests are what make the gate meaningful: they prove the thing
that would ship actually works, not just that the code compiles and its
units pass. A project with no `[build]` and no acceptance specs can still use
the gate, but the spec treats that as a degraded configuration and `orchestra
doctor` warns about it.

### 7.2 A gate run

1. **Batch.** Take up to `batch_size` `APPROVED` tickets, oldest approval
   first. Each becomes `MERGING`.
2. **Assemble.** Create a gate worktree at `<state>/gate/<run-id>` on a
   temporary branch `orchestra/gate-<run-id>` from the current
   `base_branch` head. For each ticket, `git merge --no-ff
   <reviewed_commit>`. A merge conflict is routine, not a failure: the ticket
   goes straight back to its own worker session with the conflict output and
   the instruction to merge the base branch and resolve, it does not count as
   a review round, and neither the Coordinator nor the Supervisor is told.
   Assembly continues without it.
3. **Build.** Run the build command in the gate worktree. Copy the artifact
   to `<state>/artifacts/<build-id>/` and record a `Build` with the gate
   commit it came from. A build failure is treated like a test failure for
   bisection purposes.
4. **Unit.** Run every enabled unit spec in the gate worktree. Record a
   result per spec with status, duration, failing test names when
   parseable, and the log.
5. **Start and accept.** Start the artifact per `[acceptance]`, wait for
   readiness, run every enabled acceptance spec against it, stop it. Record
   results the same way. Skipped entirely if unit failed.
6. **Land or bisect.** All pass: fast-forward `base_branch` to the gate
   commit, mark every batched ticket `DONE`, remove their worktrees, delete
   their branches, then promote the build to staging (7.5). Any failure: go
   to 7.3.
7. **Clean up.** Remove the gate worktree and branch. Record the `GateRun`.

Landing uses `git merge --ff-only` in the user's checkout when
`base_branch` is checked out there and the tree is clean; otherwise
`git update-ref` with the expected old value, so a concurrent move of `main`
fails loudly instead of being overwritten. If `push = true` in config, push
afterwards.

### 7.3 Bisection

When the batch fails:

1. **Rule out the base.** Run the failing stages on a worktree at
   `base_branch` with nothing merged. If that fails, `main` is broken: every
   ticket in the batch returns to `APPROVED`, the pipeline pauses, and the
   Pipeline Manager is woken with the log. Nothing is rejected.
2. **Bisect.** With the ordered batch and a predicate "does this subset
   reproduce the failure", recursively halve: test the first half; if it
   fails, recurse into it and the second half is tested on its own; a half
   that passes is proven safe. This is the pure `bisect_branches` function
   and is exhaustively unit tested for sizes 1 through `batch_size`. The
   predicate runs only the stages that failed: a unit failure reruns the
   selected unit tests; a build or acceptance failure reruns build, start,
   and the full acceptance specs, which is slower but the only honest test.
3. **Confirm solo.** Every branch not proven safe is tested alone on
   `base_branch`. A branch that fails alone is rejected with the failure as
   feedback. A branch that passes alone but failed in combination is
   rejected with the names of the branches it was combined with, so the
   worker can rebase and reconcile.
4. **Land the safe set.** Rebuild a gate worktree with only the proven-safe
   branches, run the full specs once more as a final check, and land as in
   7.2. If that final check fails, treat it as the base being broken.

Rejections from the gate set `rejected_by = merge_gate` and go back to the
worker like any other rejection.

### 7.4 Flaky tests

A failure that passes on an identical rerun is recorded as flaky on the
`GateRun`. The gate does not act on it. The Pipeline Manager is woken when a
test is flaky in N runs within a window, and it may call `quarantine_test`,
which marks a spec or test name as non-blocking in the project's state.
Quarantined tests still run and still report.

### 7.5 Staging

Staging is one long-lived instance of the most recent build that passed the
gate, run on this machine and supervised by `orchestra run`. It exists so
there is always a running, inspectable version of `main`, and so the
acceptance tests have proven the exact artifact that is running. When the
product is Orchestra itself, staging is a full instance (loop, resident
agents, and web UI) of the new build managing a separate demo project, so
features are tried on real tickets there before the tooling on this
repository is upgraded. The two instances use different control ports and
different projects and never share state.

When a gate run lands:

1. Record the new build as the staging candidate.
2. Stop the current staging process, if any, with the configured stop
   command or SIGTERM then SIGKILL after a grace period.
3. Start the new build with the `[staging]` start command on the configured
   port, with the artifact path and port available as placeholders.
4. Wait for the health probe to pass within the configured timeout.
5. If the service is HTTP and a tunnel is configured, ensure `cloudflared`
   is running the named tunnel against the staging port, then probe the
   public hostname's health path through the tunnel. Reuse a healthy
   running tunnel; start one if none is running; restart it if it is
   running but the public probe fails while the local probe passes.
6. Emit `staging_deployed` with build id, commit, local URL, and public URL.

If health or the tunnel probe fails, Orchestra restarts the previous build,
emits `staging_failed` with the logs, and wakes the Pipeline Manager. `main`
is not rolled back; the gate already proved the code, so a staging failure
is an environment problem for the Pipeline Manager to turn into a ticket.

Staging is also restarted on `orchestra run` startup with whatever build is
recorded as current, and can be driven by hand with `orchestra staging
status|restart|stop|logs`. Projects whose artifact is not a service, such as
a library or CLI, configure `[staging]` with a start command that installs
the artifact into a staging location and a health command that exercises
it; the tunnel section is simply absent.

Tunnel creation, credentials, and DNS are one-time operator setup with
`cloudflared tunnel create` and `cloudflared tunnel route dns`. Orchestra
only writes the ingress config for the named tunnel and runs it.
`orchestra doctor` checks that the named tunnel exists and its credentials
file is present.

---

## 8. Storage

### 8.1 Layout

```
~/.orchestrator/
  projects.json                  # repo_root -> key index
  <key>/
    project.json                 # key, repo_root, prefix, backend choice
    tickets.json                 # local backend data: tickets, milestones, notes, counters
    runtime.json                 # sessions, worktrees, gate runs, quarantine
    events.jsonl
    sessions/                    # Pi session dir for this project
    worktrees/<ticket-id>/
    gate/<run-id>/
    artifacts/<build-id>/        # built artifacts that passed the gate, plus the candidate under test
    staging/                     # staging process logs, tunnel config
    logs/<run-id>/<stage>-<spec>.log
```

Every JSON file is written atomically and guarded by a file lock so the loop,
the `orchestra api` process spawned by agent tools, and the web server never
corrupt it.

### 8.2 TicketBackend interface

```python
class TicketBackend(Protocol):
    def get_ticket(self, ticket_id) -> Ticket
    def list_tickets(self, *, state=None, milestone_id=None) -> list[Ticket]
    def create_ticket(self, draft: TicketDraft) -> Ticket            # milestone must be open
    def update_ticket(self, ticket_id, expected_version, **changes) -> Ticket   # raises Conflict
    def transition(self, ticket_id, expected_version, to_state, **fields) -> Ticket
    def add_note(self, ticket_id, note: Note) -> None
    def get_milestone(self, milestone_id) -> Milestone
    def list_milestones(self) -> list[Milestone]
    def create_milestone(self, draft) -> Milestone
    def update_milestone(self, milestone_id, expected_version, **changes) -> Milestone
    def close_milestone(self, milestone_id, expected_version) -> Milestone  # all tickets terminal; irreversible
    def sync(self) -> list[Event]   # pull external changes; no-op locally
```

The milestone rules from section 2.3 are enforced inside every backend, and
the shared conformance test suite checks them.

Implementations:

- `LocalJsonBackend` (v1).
- `GitHubIssuesBackend`, `JiraBackend`, `LinearBackend` (later). Each maps
  ticket states to a native status where one exists and to an
  `orchestra:<state>` label otherwise, stores Orchestra-only fields in a
  fenced block in the issue body, and treats the external system as the
  source of truth for title, description, and edits made in that system. `sync` runs on
  every tick.

Runtime records always use the local store regardless of ticket backend.

---

## 9. Project configuration

`orchestra.toml` at the repository root is the only thing Orchestra adds to a
project. `orchestra init` writes a starter.

```toml
[project]
base_branch = "main"
ticket_prefix = "ORC"
push = false

[agents]
provider = "ornith-1.5-35b-a3b-q6k"
model = "ornith-1.5-35b-a3b-q6k"
thinking = "medium"
workers = 2
reviewers = 1
silence_timeout = "20m"   # a turn ends after this long with nothing from the agent
# turn_timeout = "2h"    # optional hard cap per turn; unset means a busy agent is never cut off
review_rounds_before_escalation = 3

[agents.prompts]            # optional per-role overrides, paths relative to repo root
worker = ".orchestra/worker.md"

[build]
command = "uv build"                 # runs in the gate worktree
artifact = "dist/*.whl"              # glob or path, relative to the worktree
timeout = "600s"

[acceptance]                         # how to run the artifact for acceptance tests
start = "uv run --with {artifact} myapp serve --port {port}"
ready = "http://127.0.0.1:{port}/health"   # URL probe, or a shell command with `ready_command`
ready_timeout = "60s"
stop = ""                            # optional; default SIGTERM

[gate]
batch_size = 6
batch_wait = "120s"
timeout = "900s"                     # per spec run

[[gate.unit]]
name = "unit"
command = "uv run pytest tests/unit -q"
select = "uv run pytest -q {selector}"

[[gate.acceptance]]
name = "acceptance"
command = "uv run pytest tests/acceptance -q --base-url {url}"

[staging]
start = "uv run --with {artifact} myapp serve --port {port}"
port = 8080
health = "/health"
health_timeout = "60s"
stop = ""

[staging.tunnel]                     # optional; HTTP services only
name = "myapp-staging"               # existing named cloudflared tunnel
hostname = "staging.example.com"
```

Placeholders: `{artifact}` is the absolute path of the built artifact,
`{python}` an interpreter in a persistent venv created next to the artifact
with it installed (for wheels and sdists; otherwise Orchestra's own),
`{port}` the port Orchestra chose or configured, `{url}` the base URL of the
running target, `{selector}` the failing test selector.

Project conventions for agents come from the repository's own `AGENTS.md`,
which Pi reads automatically.

---

## 10. CLI

```
orchestra init                      register the cwd repo; write orchestra.toml and ~/.orchestrator/<key>
orchestra run [--once]              start the loop (workers, reviewers, gate, wake-ups)
orchestra chat                      talk to the Supervisor; streams its output; Ctrl-C to leave
orchestra web [--port]              start the web UI against this project
orchestra status                    one-screen summary: slots, counts by state, pipeline state
orchestra ticket list|show <id>     read-only
orchestra milestone list|show <id>  read-only
orchestra sessions list|show <id>   transcripts from Pi session files
orchestra gate run|show <id>        run the gate now, or inspect a run
orchestra build list|show <id>      artifacts the gate has produced
orchestra staging status|restart|stop|logs
orchestra doctor [--fix]            orphaned worktrees, branches, stale sessions, config, tunnel, build setup
orchestra api <command> --json      machine interface used by the Pi extension and by tests
```

There are no CLI commands for a human to create or edit tickets or
milestones. That happens through the Supervisor in `chat`. The `api`
subcommand is the write path for agent tools; it is not a user interface.

`run` and `web` are separate processes sharing the store. `chat` can run
alongside `run`; the Supervisor's ticket writes are picked up on the next
tick.

---

## 11. Web UI

A separate `frontend/` package, React with TypeScript and Vite, built into
static files that the FastAPI app serves. Live updates over one WebSocket
that forwards the event stream. The UI is read-only with one exception: the
Supervisor's transcript drawer has a message box, because the Supervisor is the
one agent a human talks to. That message goes to the resident Supervisor
session through the control endpoint `orchestra run` serves, the same path
`orchestra chat` uses. Two operator controls also write: pause/resume
starting new workers, and stopping a running worker or reviewer, which resets
its ticket to `OPEN` (or `READY_FOR_REVIEW`) with its branch and session kept.
These act on the orchestrator, not on ticket content. Nothing else in the UI
writes.

### 11.1 Navigation

A top bar links the two pages: **Dashboard** and **Tickets**. It also shows
the project key, base branch, and model.

### 11.2 Dashboard

Three panels, always live.

**Left: agents.** The three project agents are fixed at the top in this
order: `supervisor`, `coordinator`, `pipeline-manager`, each shown whether or
not it has a session yet, with a live dot when running, elapsed time, and the
last line it said. Below them, every running worker and reviewer, each card
carrying the ticket ID and ticket title it is working on, then a short list
of recently finished ones. Clicking any card opens the transcript drawer.

**Center: merge queue.** Pipeline paused banner in red with the reason when
applicable. Then: the gate run in progress with its batch, the ticket titles,
elapsed time, and per-stage results; the queue of `APPROVED` tickets in merge
order with how long each has waited and its reviewed commit; the tickets
upstream of the queue (`IN_PROGRESS`, `REJECTED`, `READY_FOR_REVIEW`,
`IN_REVIEW`) so the next arrivals are visible; and the recent gate run
history with outcome and summary. Clicking a run opens the gate run drawer.

**Right: system stats and staging.** Host utilization, not Orchestra
counters: a bar graph with one bar per CPU core, and beneath it on one line
the memory and disk utilization meters with used/total figures, plus core
count and load average. Under that, the **staging environment** panel: a
terminal-style view of the staging process's output, following the tail.
When staging is not configured the panel says so.

### 11.3 Tickets page

The ticket graph on its own page: milestones as labeled groups, tickets as
nodes colored by state, dependency edges, active tickets highlighted, a
filter box, and a legend of counts by state. Clicking a node opens the ticket
drawer.

### 11.4 Drawers

Drawers slide over the main screen; the URL carries their state so they can
be linked.

- **Ticket.** Fields, notes, branch, diff of the reviewed commit, review
  history, gate history, links to its worker and reviewer transcripts.
- **Transcript.** A session's transcript rendered from Pi's session file,
  streaming while the session is live.
- **Gate run.** Full detail of one run: stages, specs, logs, bisection tree,
  what landed and what was rejected, the build it produced.
- **Milestone.** Its tickets as a list with states and the counts summary.

### 11.5 API

All `GET`: `/api/state` (project, tickets, milestones, sessions, gate runs,
pipeline), `/api/tickets/{id}`, `/api/sessions/{id}/transcript`,
`/api/gate/runs/{id}`, `/api/system` (per-core CPU, memory, disk, load),
`/api/staging` (status and log tail), `/api/events?since=`, and
`WS /api/events`.

---

## 12. Package layout

```
pyproject.toml                  uv project; console script `orchestra`
orchestra/
  __init__.py
  cli.py                        argparse or typer; thin, delegates to services
  config.py                     orchestra.toml parsing, ~/.orchestrator paths, project key
  model.py                      Ticket, Milestone, Note, states, transition table
  store/
    base.py                     TicketBackend protocol, Conflict, locking helpers
    local.py                    LocalJsonBackend + runtime store
  git.py                        worktree, branch, merge, ff, update-ref wrappers
  pi/
    client.py                   RPC process client
    sessions.py                 session naming, file discovery, transcript parsing
    prompts/                    role prompts
    extension/                  the TypeScript Pi extension (tools) and its build
  agents/
    worker.py, reviewer.py, supervisor.py, coordinator.py, pipeline_manager.py
  loop.py                       tick, slots, scheduling, crash recovery
  gate/
    bisect.py                   pure bisection
    runner.py                   gate runs: assemble, build, unit, acceptance, bisect, land
    build.py                    build command, artifact capture and store
    target.py                   start/stop/ready for the acceptance target
  staging.py                    staging process supervision, health, rollback
  tunnel.py                     cloudflared named tunnel config, run, probe
  events.py
  api.py                        service functions used by cli, web, and `orchestra api`
  web/
    app.py                      FastAPI, WebSocket, static serving
frontend/                       Vite + React + TypeScript
tests/
  unit/                         model, transitions, bisect, config, store
  git/                          against throwaway repositories
  pi/                           against a fake pi process that speaks the RPC protocol
  acceptance/                   real pi + local model, opt-in via ORCHESTRA_ACCEPTANCE=1
```

No `sys.path` manipulation anywhere. One package, installed with `uv`.

---

## 13. Build plan

Each phase ends with its definition of done met and the suite green.

**Phase 1: tickets and the worker loop.**
Model, transition table, milestone rules, local store with locking, config,
project key, `init`, `status`, read-only `ticket` and `milestone` commands,
the `api` write commands, git worktrees, Pi RPC client, the extension with
`report_ticket` and `report_review`, worker and reviewer drivers, the loop
with slots and crash recovery, and a simple gate that lands one approved
branch at a time through all four stages: build, unit, start, acceptance.
Staging deploy is Phase 2.
Done when: against a scratch repository and a local model, a ticket created
through `orchestra api` goes `OPEN` to `DONE` with no human action, a deliberately
failing ticket ends `REJECTED` then `DONE` after the worker fixes it, and a
worker that cannot proceed ends `BLOCKED` with a reason.

**Phase 2: batched gate, build, and staging.**
Batching, gate worktree, the build stage and artifact store, acceptance
target start and stop, bisection with solo confirmation, base-broken
detection and pipeline pause, flaky recording, `gate run|show`, `build`,
landing by `ff-only` or `update-ref`, staging deploy with health probe and
rollback, cloudflared tunnel reuse and restart, `staging` commands.
Done when: four approved branches where one breaks a test land three and
reject one with the right feedback, in a real-git test; `main` broken pauses
the pipeline and rejects nothing; a landed gate run produces an artifact that
is running in staging and answering its health probe, and a build whose
health probe fails is rolled back to the previous one with the Pipeline
Manager woken.

**Phase 3: the project agents.**
Supervisor with `orchestra chat` and the ticket tools; Coordinator wake-ups
and prompts; Pipeline Manager wake-ups, `quarantine_test`; the event log.
Done when: a human describes a small feature in chat, the Supervisor creates
a milestone and tickets with dependencies, `run` executes them in order, and
the Supervisor can report progress when asked.

**Phase 4: web UI.**
The three-column main screen and the four drawers, live over the WebSocket.

**Phase 5: external backends.**
`GitHubIssuesBackend` first, then Jira and Linear, each behind the same
interface with a conformance test suite shared by all backends.

---

## 14. Non-goals for v1

- Multiple repositories per project, or cross-repository tickets.
- Human editing of tickets or milestones outside the Supervisor conversation.
- Remote execution; everything runs on one machine.
- Any agent merging, pushing, or editing `main`.
- Authentication on the web UI; it binds to localhost. Only staging gets a
  tunnel, and the tunnel's existence, credentials, and DNS are operator
  setup; Orchestra runs it, it does not create it.
- Production deployment. Staging is the last stop; promoting beyond it is
  outside Orchestra.
- Parallel reviewers on one ticket, or multi-reviewer consensus.
