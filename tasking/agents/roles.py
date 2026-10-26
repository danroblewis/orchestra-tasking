"""The role registry: every long-running agent kind is declared here.

Adding a role is one entry plus a prompt file in ``prompts/``. The loop keeps each
resident role's Pi session alive, wakes it on the listed events, and on a schedule
when ``every`` is set. Tools are granted by group name; the Pi extension reads the
groups from ORCHESTRA_TOOLS so no TypeScript changes are needed for a new role.

Tool groups (see harness/tools.py):
  tickets      read and write tickets and milestones, project status
  capacity     the runtime worker/reviewer capacity dial (0..configured max)
  context      read and update the project context document
  pipeline     gate runs, pipeline resume, staging status and restart
  escalate     escalate_to_supervisor
  events       read the event log
  team         read the organisation; brief ad hoc agents and message along the tree (leads, supervisor)
  roles        design the organisation: roles, agents, teams (supervisor)
  report_ticket / report_review   the worker and reviewer exit tools
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Role:
    name: str                       # ORCHESTRA_ROLE value, e.g. "coordinator"
    session_id: str                 # session id, e.g. "coordinator"
    prompt: str                     # prompt file stem under agents/prompts/
    tools: tuple[str, ...]          # tool groups
    resident: bool = True           # kept alive for the life of `tasking run`
    wake_on: tuple[str, ...] = ()   # event types this role is woken for (the loop detects them in code and wakes it)
    triggers: tuple[str, ...] = ()  # conditions that are not events: a free slot, a human message, a threshold
    emits: tuple[str, ...] = ()     # event types this role's actions cause, through its tools or the loop
    every: float | None = None      # seconds between scheduled wake-ups, or None
    description: str = ""


ROLES: dict[str, Role] = {}


def register(role: Role) -> Role:
    ROLES[role.name] = role
    return role


register(Role(
    name="supervisor", session_id="supervisor", prompt="supervisor",
    tools=("tickets", "context", "pipeline", "roles", "team", "events"),
    wake_on=("gate_base_broken", "gate_error", "staging_failed", "pipeline_paused"),
    triggers=("a message from the human", "escalate_to_supervisor from any agent"),
    emits=("ticket_created", "ticket_updated", "milestone_created", "milestone_closed", "context_updated",
           "role_created", "agent_created", "team_created", "agent_messaged"),
    description="Talks to the human; translates requests into objectives, verification, and tickets.",
))
register(Role(
    name="coordinator", session_id="coordinator", prompt="coordinator",
    tools=("tickets", "capacity", "escalate"), every=10 * 60,
    wake_on=("ticket_created", "ticket_transition:DONE", "ticket_transition:BLOCKED", "milestone_created", "milestone_closed"),
    triggers=("a ticket rejected at the review threshold",),
    emits=("ticket_updated", "ticket_transition:CANCELLED", "note_added", "capacity_changed"),
    description="Keeps the plan executable: clarity, size, order, dependencies. Never changes intent.",
))
# Per-ticket roles are not resident; the loop starts them on demand.
register(Role(name="worker", session_id="worker-{ticket}", prompt="worker", tools=("report_ticket",), resident=False,
              wake_on=("ticket_transition:IN_PROGRESS", "ticket_transition:REJECTED"),
              triggers=("an OPEN ticket whose dependencies are DONE and whose milestone is open", "a free worker slot"),
              emits=("ticket_transition:READY_FOR_REVIEW", "ticket_transition:BLOCKED"),
              description="Implements one ticket on its own branch and reports READY_FOR_REVIEW or BLOCKED."))
register(Role(name="reviewer", session_id="reviewer-{ticket}", prompt="reviewer", tools=("report_review",), resident=False,
              wake_on=("ticket_transition:READY_FOR_REVIEW",), triggers=("a free reviewer slot",),
              emits=("ticket_transition:APPROVED", "ticket_transition:REJECTED"),
              description="Reviews one ticket's diff like a pull request and reports APPROVED or REJECTED."))

# Not agents, but parts of the same event graph (see orchestra.web.app eventmap):
SYSTEM_BINDINGS = {
    "orchestra": {"description": "The loop itself: sessions, turns, crashes, compaction, and the ticket state machine.",
                  "emits": ("ticket_transition:OPEN", "ticket_transition:IN_PROGRESS", "ticket_transition:IN_REVIEW", "session_started", "session_settled",
                            "turn_timed_out", "turn_interrupted", "agent_crashed", "agent_compacted", "agent_reset",
                            "pickup_paused", "pickup_resumed", "loop_started", "loop_stopped"), "wake_on": ()},
    "merge_gate": {"description": "Lands approved branches: build, unit, start, acceptance; then staging.",
                   "emits": ("gate_started", "gate_landed", "gate_rejected", "gate_conflict", "gate_error", "gate_base_broken",
                             "ticket_transition:MERGING", "ticket_transition:DONE", "ticket_transition:REJECTED",
                             "staging_deployed", "staging_failed", "pipeline_paused"),
                   "wake_on": ("ticket_transition:APPROVED",)},
    "probes": {"description": "Commands run on an interval; an event on each change of result.",
               "emits": ("probe_failed", "probe_recovered"), "wake_on": ()},
}

# Future roles slot in here, for example:
#   register(Role(name="product_owner", session_id="product-owner", prompt="product_owner",
#                 tools=("tickets", "context", "escalate"), every=24 * 3600,
#                 description="Daily review of objectives versus landed work."))
#   register(Role(name="researcher", ..., every=7 * 24 * 3600))


def resident_roles() -> list[Role]:
    return [r for r in ROLES.values() if r.resident]
