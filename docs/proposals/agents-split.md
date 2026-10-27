# The task system and the agents, apart

Status: done, 2026-10-26. Two repositories, each an Orchestra plugin, meeting only on
Orchestra's bus: orchestra-tasking (this one, plugin `tasks`) and orchestra-agents (plugin
`agents`). This note keeps the reasoning and says what was done; `SPEC.md` describes the task
system as it is.

## Why

The task system holds tasks and decides who does what next. AI agents are one kind of machine
that can do a task. Until 2026-10-25 the two were one program: the scheduler assigned a ticket
by starting a Claude Code or Pi session itself, in a thread, and read the agent's report from a
shared store. So the task system could not assign work to anything but an AI agent, and the
agents could not grow without growing the task system with them.

The test of the split: **the task system works when none of its machines is an AI agent**, and
the agents package works for a task system that is not ours.

## What went where

| | orchestra-tasking | orchestra-agents |
|---|---|---|
| owns | tickets and milestones, states and dependency graph, readiness, priority (aging, inheritance); roles as names with a capacity and a lease; assignment and ownership; the merge gate, worktrees and staging for code; probes; the Tickets page, the Tasks panel, the blocked-ticket banner; the ticket metrics (since 2026-10-27 read by an instrument the plugin declares; the scheduler's machine card is gone) | the AI agents: their roles, prompts, harnesses, sessions and the host that runs them |
| state | `~/.orchestrator/<key>/tasks/` | `~/.orchestrator/<key>/agents/` |
| config | `[project]`, `[tasks]`, `[build]`, `[acceptance]`, `[gate]`, `[staging]`, `[[probes]]` | `[agents]` (and `[project]` base_branch, ticket_prefix, work, read for its prompts) |

Neither imports the other. The agents package does no branching or worktree management (the
task system prepares the workspace and names it in the offer) and changes no ticket itself:
every change is a request on the bus that the task system applies.

## The protocol

Everything goes over Orchestra's bus as CloudEvents, so Orchestra observes the coordination (the
lesson of Beads: publish decisions, not storage changes). The ticket is the `subject`.

- **Assignment**: `task.assigned` from the scheduler to a role; `task.accepted`, `task.working`,
  `task.reported` from the machine instance that took it; `task.released` from the scheduler.
  (Since 2026-10-27 the scheduler is no machine: it publishes `task.assigned` and `task.released`
  as the role's queue, `worker` or `reviewer`, and attention as the machine whose work is stuck;
  SPEC.md section 3, "Who publishes what".)
  The table with every field is the docstring of `tasking/assignments.py`; the agents implement
  the same names in `orchestra_agents/protocol.py`.
- **Requests**: `task.requested`, `task.change_requested`, `task.note_requested`,
  `task.cancel_requested`, `task.unblock_requested`, `milestone.requested`,
  `milestone.change_requested`, `milestone.close_requested`, `capacity.change_requested`,
  `pipeline.resume_requested`, and `task.query_requested` with an `op`; answered with
  `request.done` or `request.refused` (`tasking/requests.py`).
- **Attention**: `task.attention_needed` {to: role, reason, ticket?} from the task system to a
  planner role (`coordinator`, `supervisor`): names, which the agents package gives programs.
- **Announcement**: `orchestra.machine.started` from every machine; Orchestra's Machines page
  reads `mode`, `listens_for`, `log` and `package` and passes the rest to the package's card.

## When the machines are not AI agents

| Role | As an AI agent (orchestra-agents) | As something else |
|---|---|---|
| Supervisor (the human's way in) | as there | a script that sends requests (`tasking api`, `demo/run.py`) |
| Coordinator | as there | none: the task system runs without one; its attention events go unanswered |
| Worker | as there | `demo/machines/shell_worker.py`: a ticket's ```` ```sh ```` blocks, run in its workspace |
| Reviewer | as there | `demo/machines/check_reviewer.py`: RESULT.md and the acceptance text's files and lines |

A person as a worker or reviewer would be a human acting other than through a planner, which
Orchestra's rule 9 does not allow today; human roles wait on that decision.

## Steps, as they went

1. The protocol inside orchestra-tasking: assign and apply over the bus, leases, releases;
   planners' changes as requests (commits `885fe5f`, `1df3367`, `cc3f170`).
2. orchestra-agents created with everything in its column, its own tests, meeting the task
   system only on the bus.
3. orchestra-tasking stripped of the agents (`344364d`), then of every last trace of them: no
   session in an acceptance or on a ticket, no `[agents]` compatibility, no harness options in
   the CLI (`f586dfc`).
4. The demo with two programs filling the roles, and an e2e test that runs it through a real
   Orchestra (`f523797`).

Later, and not decided: Orchestra's lease-aware subscriptions taking over the leases;
`orchestra run` starting the plugins' processes. The gate lands one ticket per run; batching was
taken out rather than kept unused.
