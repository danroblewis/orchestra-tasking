# Proposal: the task system and the agents, apart

Status: proposal, 2026-10-25. Nothing here is built.

## 1. Why

The task system holds tasks and decides who does what next. AI agents are one kind of machine
that can do a task. Today the two are one program: the scheduler (`tasking/loop.py`) assigns a
ticket by starting a Claude Code or Pi session itself, in a thread, and reads the agent's report
from a shared store. So the task system cannot assign work to anything but an AI agent, and the
agents cannot grow (system dynamics tools, simulation, proposing and checking changes) without
growing the task system with them.

The goal: two packages, each an Orchestra plugin, that meet only through Orchestra's bus.

| Package | Owns |
|---|---|
| **orchestra-tasking** | Tasks and milestones, their states and dependency graph, readiness and topological order, priority (aging, inheritance) and later leverage measures; **roles to fill** and how many of each (capacity); assignment and ownership; leases (no zombies); datetime schedules of machines (wake a machine every 10 minutes, daily at 09:00); the ticket metrics; the Tickets page and the Tasks panel |
| **orchestra-agents** | AI agents as machines: harnesses (Claude Code, Pi, Codex), sessions (resident, ad hoc, resumed), transcripts, prompts and prompt templates, context, tools, roles as job descriptions, teams; the Supervisor chat; the Agents page; later the system dynamics tools, simulation and experiments |

The test of the split: **the task system works when none of its machines is an AI agent**, and
the agents package works for a task system that is not ours.

## 2. What is coupled today

| In `tasking/` now | Belongs to |
|---|---|
| `model.py`, `store/` (tickets, milestones, notes, capacity, pause, kills, ownership), `measures.py`, `probes.py`, `events.py`, `config.py` ([project]) | tasking |
| `loop.py` `tick`, readiness, priorities, capacity, `_work`/`_review` as assignment, `_apply_worker_report`, `_verdict`, `_reject`, recovery | tasking (rewritten to assign over the bus) |
| `loop.py` sessions: `_open_session`, residents, `_wake*`, `_schedule_wakeups` (the agent part), compaction, crash loops, `_on_event`, transcript markers | agents |
| `harness/`, `agents/` (prompts, built-in roles), `roles.py`, `org.py`, `api_org.py`, `control.py` (Supervisor chat) | agents |
| `api.py` (the tools agents call: create, update, report, unblock …) | split: the **requests** go to tasking over the bus; the **tool definitions** stay with agents |
| `gate/`, `staging.py`, `git.py` (`work = "code"`) | tasking for now; later its own package with pull request trees |
| web: Tickets page, Tasks panel, `/board`, ticket and attention (blocked) endpoints | tasking |
| web: Agents page, `/eventmap`, transcripts, Supervisor chat, agent machine cards, the Supervisor button | agents |

## 3. The protocol between them

Everything goes over Orchestra's bus as CloudEvents, so Orchestra observes the coordination
(the lesson of Beads: publish decisions, not storage changes). The ticket is the `subject`.

**Assignment** (the scheduler fills a role):

| Event | From | Data |
|---|---|---|
| `task.assigned` | task_scheduler | role (`worker`, `reviewer`, …), assignment id, the brief (title, objective, description, acceptance, verification, feedback from a rejection), the workspace, lease seconds |
| `task.accepted` | the assignee (`worker/1`) | assignment id |
| `task.working` | the assignee | assignment id (a heartbeat: extends the lease) |
| `task.reported` | the assignee | assignment id; for work: `ready` or `blocked` with a summary or reason; for review: `approved` or `rejected` with feedback |
| `task.released` | task_scheduler | assignment id, why (lease expired, the operator stopped it): the ticket goes back to waiting |

The scheduler applies every transition itself (the old rule "only the task system changes ticket
state" holds) and owns the leases: no `task.working` before the lease ends means the assignee is
gone, and the ticket goes back. Which machine answers is decided by who subscribes for that
role; capacity is how many assignments of a role may be out at once.

**Requests** (anyone who plans: the Supervisor, the Coordinator, a person, a script):
`task.requested` (create), `task.change_requested`, `task.unblock_requested`,
`task.cancel_requested`, `milestone.requested`. The scheduler validates each and answers with
what happened (`ticket_created`, a transition, or `request_refused` with the reason). Reads stay
an HTTP API (`/api/plugins/tasks/...`), which agents' tools call.

**Schedules**: a machine that runs on a clock (the Coordinator every 10 minutes) is woken by the
task system's scheduler with `machine.due` {machine, reason}. Wake-ups on events need nothing:
the machine subscribes to the events.

## 4. When the machines are not AI agents

| Role | As an AI agent (orchestra-agents) | As something else |
|---|---|---|
| Supervisor (the human's way in) | as now | a program that turns the CLI and the chat box into `task.requested` events; or none, with the Tickets page offering "new task" |
| Coordinator | as now | deterministic checks (dependencies on cancelled tickets, a milestone whose tickets are all done) as a small program; or none: the task system runs without one |
| Worker | as now | a script per kind of task (`task.assigned` → run a command in the workspace → `task.reported`); a person, through a work queue page |
| Reviewer | as now | automatic checks (a test, a schema); a person, with approve and reject on the ticket |

What the task system must not assume, and today does: that an assignee has a session, a
transcript, a prompt or a harness; that a report comes from a tool call; that the Supervisor
exists; that roles are AI roles. Roles in the task system become plain names with a capacity
and a lease; their job descriptions, prompts and teams are the agents package's.

Open question: a person as a worker or reviewer is a human acting in the system other than
through the Supervisor, which Orchestra's rule 9 does not allow today. Either that rule becomes
"humans act only through machines that say so" or human roles wait.

## 5. Steps

1. **The protocol inside orchestra-tasking.** The scheduler assigns by publishing `task.assigned`
   and applies `task.reported`; an adapter in the same process runs the agents on assignments,
   as today. The tests run the whole flow through the bus. Nothing moves yet.
2. **A non-AI worker** (a script that answers `task.assigned` for a role) and the demo project
   using it, to prove the task system no longer needs agents.
3. **orchestra-agents**, a new repository and plugin: the harnesses, sessions, prompts, roles,
   teams, Supervisor chat and Agents page move there, with their tests; it runs as its own
   process (`agents run`), subscribing for the roles it serves. orchestra-tasking loses them.
4. **The demo installs three packages** and works as before, then with the script worker in
   place of the AI worker.
5. Later: Orchestra's lease-aware subscriptions take over the leases; `orchestra run` starts
   the plugins' processes; the agents get the system dynamics tools.
