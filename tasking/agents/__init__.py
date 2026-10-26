"""Role prompts and per-turn task prompts for the five agent kinds."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import Project
from ..model import State, Ticket

PROMPTS_DIR = Path(__file__).parent / "prompts"


def role_prompt(project: Project, role: str) -> str:
    """A role's own prompt, placeholders resolved: the job description. Built-in roles
    ship with Orchestra (overridable per project); custom roles come from the role store."""
    override = project.config.prompt_overrides.get(role)
    path = (project.repo_root / override) if override else (PROMPTS_DIR / f"{role}.md")
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        from ..roles import role_store
        text = role_store(project).prompt(role)
        if not text.strip():
            text = f"You are good at being the {role}. Act on the wake-up you receive using your tools, then summarize."
    return _fill(project, text)


def _fill(project: Project, text: str) -> str:
    return text.replace("{base_branch}", project.config.base_branch).replace("{ticket_prefix}", project.config.ticket_prefix)


def agent_prompt(project: Project, agent, roles: dict | None = None, teams: dict | None = None) -> str:
    """The system prompt an agent is started with: its roles' prompts, then its team's
    responsibilities and those of the teams above it, then its access. Built-in agents
    outside a custom team get exactly their role prompt, as before teams existed."""
    from ..org import all_teams, org_store, team_chain, tools_path
    from ..roles import all_roles
    roles = roles if roles is not None else all_roles(project)
    teams = teams if teams is not None else all_teams(project)
    chain = team_chain(teams, agent.team)
    if agent.builtin and all(t.builtin for t in chain):
        return role_prompt(project, agent.roles[0])
    team = chain[0] if chain else None
    lead_of = [t.name for t in teams.values() if t.lead == agent.name]
    if team is None and agent.team:
        team_line = (f"You are assigned to the team `{agent.team}`, which has not been created yet; until it is, the "
                     "Supervisor is your lead and you have no team responsibilities.")
        reach_line = "The human, and decisions above you, are reached only through `escalate_to_supervisor`."
    elif team is None:
        team_line = "You belong to no team; you report to the Supervisor."
        reach_line = "The human, and decisions above you, are reached only through `escalate_to_supervisor`."
    elif team.name in lead_of:
        members = [a for a in team.agents if a != agent.name]
        parent_lead = teams[team.parent].lead if team.parent and team.parent in teams else None
        team_line = f"You belong to the team `{team.name}`, which you lead."
        reach_line = ("Your members" + (f" ({', '.join(f'`{m}`' for m in members)})" if members else "") +
                      " are reached with `message_agent`, and ad hoc members are briefed with `run_agent`. " +
                      (f"You report to `{parent_lead}`, the lead of the team above yours, with `message_agent`; "
                       if parent_lead else "You have no lead above you except the Supervisor; ") +
                      "the human, and decisions you cannot make, are reached only through `escalate_to_supervisor`.")
    else:
        team_line = f"You belong to the team `{team.name}`, led by `{team.lead}`."
        reach_line = (f"Your lead `{team.lead}` is reached with `message_agent`; the human, and decisions above your lead, "
                      "only through `escalate_to_supervisor`.")
    parts = [AGENT_PREAMBLE.format(agent=agent.name, roles=", ".join(f"`{r}`" for r in agent.roles), team_line=team_line,
                                   reach_line=reach_line)]
    for r in agent.roles:
        parts.append(f"## Your role: {r}\n\n{role_prompt(project, r).strip()}\n")
    for i, t in enumerate(chain):
        resp = "" if t.builtin else org_store(project).responsibilities(t.name).strip()
        rel = "your team" if i == 0 else f"the team above `{chain[i - 1].name}`"
        who = f"Lead: `{t.lead}`. Agents: {', '.join(f'`{a}`' for a in t.agents) or 'none'}."
        subs = [x.name for x in teams.values() if x.parent == t.name]
        if subs:
            who += f" Sub-teams: {', '.join(f'`{x}`' for x in subs)}."
        body = _fill(project, resp) if resp else "(none written yet)"
        parts.append(f"## Team `{t.name}` ({rel})\n\n{t.description}\n{who}\n\n### Responsibilities of `{t.name}`\n\n{body}\n")
    if team:
        tools_dirs = ", ".join(f"`{p.relative_to(project.repo_root)}`" for p in tools_path(project, teams, agent))
        lines = [f"- Working directory and space: `{team.space}` (yours to organise; keep it accurate and small)",
                 f"- Tools on your PATH: {tools_dirs}"]
        for t in chain:
            for g in t.paths:
                lines.append(f"- {g['access']}: `{g['path']}`" + (f" (granted to team `{t.name}`)" if t.name != team.name else ""))
        lines.append("- Everything else in the repository is read-only to you. Code changes land only through tickets and the merge gate.")
        parts.append("## Your access\n\n" + "\n".join(lines) + "\n")
    return "\n".join(parts)


def org_guide() -> str:
    """The Supervisor's guide to designing roles, agents, and teams; loaded on demand."""
    return (PROMPTS_DIR / "org_guide.md").read_text(encoding="utf-8")


CONTEXT_TEMPLATE = """# Project context

_Maintained by the Supervisor. Every Supervisor turn starts with this document._

## Objectives
(What this project is trying to achieve, in priority order.)

## Value propositions
(Who benefits and how.)

## Non-goals
(What we deliberately will not do.)

## Definition of done
A ticket is DONE when its branch has landed on the base branch through the merge gate.

## Verification
(Which acceptance tests exist, what they cover, and what a new feature must add.)
"""


def context_path(project: Project) -> Path:
    return project.state_dir / "PROJECT.md"


def read_context(project: Project) -> str:
    p = context_path(project)
    if not p.exists():
        seed = project.repo_root / ".orchestra" / "PROJECT.md"
        p.write_text(seed.read_text(encoding="utf-8") if seed.exists() else CONTEXT_TEMPLATE, encoding="utf-8")
    return p.read_text(encoding="utf-8")


def write_context(project: Project, text: str) -> None:
    context_path(project).write_text(text, encoding="utf-8")


AGENT_PREAMBLE = """# You are the `{agent}` agent of Orchestra

You are an agent in a software project run by Orchestra, with the role{plural} {roles}. {team_line}
Orchestra wakes you with a message that says why (an event, a schedule, or a note or brief from
another agent) and gives you a snapshot of the project. Act using your tools, then reply with a
short summary of what you did and why. You never write code on the base branch and never start
agents yourself; code changes happen by creating or editing tickets, which the Coordinator
orders and workers implement. {reach_line}
""".replace("{plural}", "")


def _ticket_block(t: Ticket) -> str:
    deps = ", ".join(t.depends_on) or "none"
    return (f"Ticket: {t.id}\nTitle: {t.title}\nPriority: {t.priority}\nDepends on: {deps}\n\n"
            f"## Objective (why)\n{t.objective.strip() or '(not stated)'}\n\n"
            f"## Description\n{t.description.strip() or '(none)'}\n\n"
            f"## Acceptance criteria\n{t.acceptance.strip() or '(none given; use the description)'}\n\n"
            f"## Verification (how the merge gate proves it)\n{t.verification.strip() or '(none stated; existing tests must keep passing)'}\n")


def worker_task(project: Project, t: Ticket, unit_command: str | None) -> str:
    test_line = f"Run the unit tests with: `{unit_command}`" if unit_command else "There is no configured unit test command."
    head = f"# Your assignment\n\n{_ticket_block(t)}\n"
    env = (f"## Environment\nYou are in a git worktree checked out on branch `{t.branch}`. "
           f"The base branch is `{project.config.base_branch}`. Only this branch is yours. "
           f"{test_line}\n\n")
    if t.feedback and t.rejected_by:
        return (head + env + f"## This ticket was REJECTED by the {t.rejected_by} (round {t.review_rounds})\n"
                f"Address every point below, commit, re-run the tests, then call `report_ticket`.\n\n"
                f"{t.feedback.strip()}\n")
    if t.feedback:
        return head + env + f"## Note from the planner\n{t.feedback.strip()}\n\nImplement the ticket, then call `report_ticket`.\n"
    return (head + env + "Implement the ticket. If the verification section names a test to add or extend, "
            "that test is part of the work. Verify, commit on your branch, then call `report_ticket`.\n")


def reviewer_task(project: Project, t: Ticket, commit: str, diff: str, files: list[str],
                  log: str, test_output: str | None) -> str:
    diff = diff if len(diff) < 60000 else diff[:60000] + "\n... (diff truncated; open files in the worktree for the rest)\n"
    tests = f"## Unit test output (already run by Orchestra)\n```\n{test_output.strip()[-4000:]}\n```\n" if test_output else \
        "## Unit tests\nNo unit test command is configured.\n"
    return (f"# Review request\n\n{_ticket_block(t)}\n"
            f"## Commit under review\n{commit}\n\n## Commits on the branch\n```\n{log.strip()}\n```\n\n"
            f"## Changed files\n```\n" + "\n".join(files) + "\n```\n\n"
            f"{tests}\n## Diff against `{project.config.base_branch}`\n```diff\n{diff}\n```\n\n"
            "Review it like a pull request. Check that the verification named in the ticket was actually "
            "added or extended. Then call `report_review` exactly once.\n")


def org_reference() -> str:
    """What an agent can watch and do, appended to the design guide."""
    from ..roles import EVENT_TYPES, TOOL_GROUPS
    return ("Tool groups: " + ", ".join(TOOL_GROUPS) + ".\nEvent types that can wake an agent: " + ", ".join(EVENT_TYPES)
            + ".\nA wake_on entry may filter transitions, e.g. 'ticket_transition:DONE' or 'ticket_transition:BLOCKED'.")


def wake_task(target: str, reasons: list[str], snapshot: dict) -> str:
    counts = ", ".join(f"{k}={v}" for k, v in snapshot["counts"].items() if v)
    lines = [f"# Wake-up for the {target.replace('_', ' ')}", "", "## Why you were woken"]
    shown = reasons[:12]
    lines += [f"- {r}" for r in shown]
    if len(reasons) > len(shown):
        lines.append(f"- ...and {len(reasons) - len(shown)} more of the same kinds; the snapshot below is current")
    lines += ["", "## Execution health"] + execution_health(snapshot)
    lines += ["", "## Project snapshot", f"Ticket counts: {counts or 'none'}",
              f"Pipeline: {'PAUSED: ' + str(snapshot['pipeline'].get('reason')) if snapshot['pipeline'].get('paused') else 'running'}",
              "", "Milestones:"]
    for m in snapshot["milestones"]:
        lines.append(f"- {m['id']} [{m['status']}] {m['name']}")
    lines += ["", "Tickets (id, state, priority, milestone, title):"]
    for t in snapshot["tickets"]:
        if t["state"] in (State.DONE.value, State.CANCELLED.value):
            continue
        extra = f" BLOCKED: {t['blocked_reason']}" if t["state"] == "BLOCKED" else ""
        extra += f" (review rounds: {t['review_rounds']})" if t["review_rounds"] else ""
        obj = f" | objective: {t['objective'][:80]}" if t.get("objective") else ""
        lines.append(f"- {t['id']} {t['state']} p{t['priority']} {t['milestone_id']} {t['title']}{extra}{obj}")
    lines += ["", "Use your tools to act, then report the way your role says to; if it does not say, reply with a few "
              "lines on what you did and why."]
    return "\n".join(lines)


def execution_health(snapshot: dict) -> list[str]:
    """Plain statements about whether work is flowing, with the likely cause when not."""
    cap = snapshot.get("capacity", {})
    running = [s for s in snapshot["sessions"] if s.get("status") == "running"]
    workers = [s["id"] for s in running if s.get("kind") == "worker"]
    reviewers = [s["id"] for s in running if s.get("kind") == "reviewer"]
    ready = [f"{t['id']}(p{t.get('effective_priority', t['priority'])})" for t in snapshot["tickets"] if t["state"] == "OPEN" and t.get("ready")]
    waiting = [t for t in snapshot["tickets"] if t["state"] == "OPEN" and not t.get("ready")]
    blocked = [t["id"] for t in snapshot["tickets"] if t["state"] == "BLOCKED"]
    out = [f"Worker capacity: {cap.get('workers')} of a configured maximum {cap.get('max_workers')}"
           + (f" (runtime dial set: {cap.get('note') or 'no note'})" if cap.get("dial_set") else ""),
           f"Reviewer capacity: {cap.get('reviewers')} of {cap.get('max_reviewers')}",
           f"Workers running: {len(workers)} {workers or ''}".rstrip(), f"Reviewers running: {len(reviewers)}",
           f"Ready tickets (OPEN, deps DONE, milestone open): {len(ready)} {ready[:8]}",
           f"OPEN but not ready: {len(waiting)}" + (f" e.g. {waiting[0]['id']}: {waiting[0].get('why_waiting')}" if waiting else ""),
           f"BLOCKED: {len(blocked)} {blocked[:8]}",
           f"Pipeline: {'PAUSED: ' + str(snapshot['pipeline'].get('reason')) if snapshot['pipeline'].get('paused') else 'running'}"]
    if ready and not workers:
        if cap.get("workers", 0) == 0 and cap.get("max_workers", 0) == 0:
            out.append("DIAGNOSIS: work is stalled because [agents].workers = 0 in orchestra.toml. Only the human "
                       "can change that file: escalate_to_supervisor asking for it to be raised.")
        elif cap.get("workers", 0) == 0:
            out.append("DIAGNOSIS: work is stalled because the runtime worker capacity dial is 0. "
                       "Raise it with set_capacity if there is no reason to keep it paused.")
        else:
            out.append("DIAGNOSIS: tickets are ready and capacity exists but no worker is running; the loop should "
                       "pick them up within a tick. If this persists across check-ins, escalate_to_supervisor.")
    elif not ready and not workers and waiting:
        out.append("DIAGNOSIS: nothing is ready. Check whether the waiting tickets' dependencies or milestones are "
                   "wrong and fix them; unblock BLOCKED tickets you can resolve.")
    return out


def supervisor_context(snapshot: dict, events: list[dict], project: Project | None = None, store=None) -> str:
    """What Orchestra puts in front of a Supervisor turn: the project context document only
    when it has changed since the Supervisor last saw it (the session remembers earlier
    copies, and `get_project_context` fetches it any time), then the events since its
    last turn, at most forty. A human's "yes, go ahead" should arrive as itself."""
    out = ""
    if project is not None:
        text = read_context(project).strip()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        seen = store.agent_state("supervisor").get("context_hash") if store is not None else None
        if digest != seen:
            out += "[Orchestra: project context" + (", updated" if seen else "") + "]\n" + text + "\n\n"
            if store is not None:
                store.set_agent_state("supervisor", context_hash=digest)
    if not events:
        return out
    lines = ["[Orchestra: events since your last turn]"]
    for e in events[-40:]:
        parts = [e["type"]]
        for k in ("ticket", "milestone", "from", "to", "reason", "by", "summary"):
            if k in e and e[k]:
                parts.append(f"{k}={e[k]}")
        lines.append("- " + " ".join(str(p) for p in parts))
    counts = ", ".join(f"{k}={v}" for k, v in snapshot["counts"].items() if v)
    lines.append(f"[Orchestra: ticket counts now: {counts or 'none'}]")
    return out + "\n".join(lines) + "\n\n"
