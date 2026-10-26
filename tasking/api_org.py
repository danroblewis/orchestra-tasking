"""Organisation commands: roles, agents, and teams. Called from ``tasking.api.handle``.

Creating, changing, and deleting any of the three is the Supervisor's alone (and the
human's, through the web UI, which acts as the Supervisor). Reading is open to any agent
with the `team` or `roles` tool group. Messages and briefs follow the tree enforced by
``tasking.org.may_message``."""

from __future__ import annotations

import time
from typing import Any

from . import agents
from .config import Project
from .harness.tools import tools_for
from .org import (
    AgentDef,
    OrgError,
    TeamDef,
    all_agents,
    all_teams,
    custom_event_types,
    effective_tools,
    may_message,
    members_of,
    org_store,
    team_chain,
)
from .roles import EVENT_TYPES, RoleDef, RoleError, all_roles, parse_interval, role_store
from .store import LocalStore, StoreError

ORG_COMMANDS = frozenset({
    "org_guide", "list_roles", "get_role", "create_role", "update_role", "delete_role",
    "list_agents", "get_agent", "create_agent", "update_agent", "delete_agent",
    "pause_agent", "resume_agent", "restart_agent", "reset_agent", "run_agent", "message_agent",
    "list_teams", "get_team", "create_team", "update_team", "delete_team", "emit_event",
})
EVENT_TYPES_SET = frozenset(EVENT_TYPES)
EMIT_DEPTH_LIMIT = 3          # an event may cause a wake-up that causes an event ... this many times
EMIT_RATE_LIMIT = 20          # emissions per agent per hour
MUTATIONS = frozenset({"create_role", "update_role", "delete_role", "create_agent", "update_agent", "delete_agent",
                       "create_team", "update_team", "delete_team"})


def handle_org(project: Project, store: LocalStore, command: str, a: dict[str, Any], author: str, role: str) -> dict:
    if command in MUTATIONS and role and role != "supervisor":
        raise StoreError("only the supervisor may create, change, or delete roles, agents, or teams")
    try:
        return _handle(project, store, command, a, author, role)
    except (RoleError, OrgError) as e:
        raise StoreError(str(e))


def _handle(project: Project, store: LocalStore, command: str, a: dict[str, Any], author: str, role: str) -> dict:
    rs, os_ = role_store(project), org_store(project)

    if command == "org_guide":
        return {"ok": True, "guide": agents.org_guide() + "\n\n## Reference\n\n" + agents.org_reference()}

    # -- roles: job descriptions --------------------------------------------------------
    if command == "list_roles":
        used: dict[str, list[str]] = {}
        for ag in all_agents(project).values():
            for r in ag.roles:
                used.setdefault(r, []).append(ag.name)
        return {"ok": True, "roles": [{**r.to_dict(), "builtin": r.builtin, "agents": used.get(r.name, [])}
                                      for r in all_roles(project).values()]}
    if command == "get_role":
        r = all_roles(project).get(a["name"])
        if r is None:
            raise StoreError(f"no role {a['name']!r}")
        return {"ok": True, "role": {**r.to_dict(), "builtin": r.builtin},
                "agents": [ag.name for ag in all_agents(project).values() if r.name in ag.roles], **role_definition(project, r)}
    if command in ("create_role", "update_role"):
        if command == "update_role":
            r = rs.get(a["name"])
            for k in ("description", "tools"):
                if k in a and a[k] is not None:
                    setattr(r, k, a[k])
            prompt = a.get("prompt") if a.get("prompt") else rs.prompt(a["name"])
        else:
            r = RoleDef(name=a["name"], description=a.get("description", ""), tools=list(a.get("tools") or ["tickets", "escalate"]),
                        created_by=author)
            prompt = a.get("prompt") or ""
            if len(prompt.strip()) < 40:
                raise StoreError("prompt is required: the job description, in a few paragraphs")
        rs.save(r, prompt)
        store.events.emit("role_changed" if command == "update_role" else "role_created", role=r.name, by=author)
        for ag in all_agents(project).values():
            if r.name in ag.roles and not ag.builtin and command == "update_role":
                store.add_wakeup(ag.name, f"your role {r.name} was updated; re-read your responsibilities")
        return {"ok": True, "role": r.to_dict(), "storage": rs.last_commit_note}
    if command == "delete_role":
        users = [ag.name for ag in all_agents(project).values() if a["name"] in ag.roles and ag.source == "agent"]
        if users:
            raise StoreError(f"role {a['name']!r} is used by agents {users}; change or delete them first")
        rs.delete(a["name"])
        store.set_agent_state(a["name"], deleted=True)   # an implicit Stage 1 agent of the same name stops too
        store.events.emit("role_deleted", role=a["name"], by=author)
        return {"ok": True, "storage": rs.last_commit_note}

    # -- agents: what runs ----------------------------------------------------------------
    if command == "list_agents":
        teams = all_teams(project)
        return {"ok": True, "agents": [agent_view(project, store, ag, teams) for ag in all_agents(project).values()]}
    if command == "get_agent":
        ag = all_agents(project).get(a["name"])
        if ag is None:
            raise StoreError(f"no agent {a['name']!r}")
        return {"ok": True, **agent_definition(project, store, ag)}
    if command in ("create_agent", "update_agent"):
        existing = all_agents(project)
        if command == "update_agent":
            ag = existing.get(a["name"])
            if ag is None or ag.builtin:
                raise StoreError(f"no custom agent {a['name']!r}")
            for k in ("roles", "team", "kind", "session", "wake_on", "every", "at", "description"):
                if k in a and a[k] is not None:
                    setattr(ag, k, a[k] or None if k in ("every", "at", "team") else a[k])
        else:
            if a["name"] in existing and existing[a["name"]].source == "agent":
                raise StoreError(f"agent {a['name']!r} exists; use update_agent")
            ag = AgentDef(name=a["name"], roles=list(a.get("roles") or []), team=a.get("team") or None,
                          kind=a.get("kind") or "continuous", session=a.get("session") or "resident",
                          wake_on=list(a.get("wake_on") or []), every=a.get("every") or None, at=a.get("at") or None,
                          description=a.get("description", ""), created_by=author)
        roles = all_roles(project)
        missing = [r for r in ag.roles if r not in roles]
        if missing:
            raise StoreError(f"unknown roles {missing}; create them first with create_role")
        os_.save_agent(ag, custom_event_types(all_teams(project)))
        store.set_agent_state(ag.name, deleted=False)
        store.events.emit("agent_changed" if command == "update_agent" else "agent_created", agent=ag.name, by=author)
        if ag.kind != "ad_hoc":
            store.add_wakeup(ag.name, "you were just created; introduce yourself in one line and check the project state"
                             if command == "create_agent" else "your definition was updated; re-read your responsibilities")
        out = {"ok": True, "agent": ag.to_dict(), "storage": os_.last_commit_note}
        teams = all_teams(project)
        if ag.team and ag.team not in teams:
            out["warning"] = (f"WARN: team {ag.team!r} does not exist. Create it next with create_team (lead or members "
                              f"naming {ag.name!r}), or move the agent with update_agent to one that exists: "
                              f"{sorted(teams)}. Until then it runs without team responsibilities or access.")
        return out
    if command == "delete_agent":
        ag = all_agents(project).get(a["name"])
        if ag is None or ag.builtin:
            raise StoreError(f"no custom agent {a['name']!r}")
        leads = [t.name for t in all_teams(project).values() if t.lead == ag.name]
        if leads:
            raise StoreError(f"{ag.name} leads {leads}; give those teams another lead first")
        if ag.source == "agent":
            os_.delete_agent(ag.name)
        else:
            rs.delete(ag.name)   # a Stage 1 role record that was its own agent
        store.set_agent_state(a["name"], deleted=True)
        store.events.emit("agent_deleted", agent=a["name"], by=author)
        return {"ok": True, "storage": os_.last_commit_note}
    if command in ("pause_agent", "resume_agent"):
        store.set_agent_state(a["name"], paused=(command == "pause_agent"), crash_loop=None, crashes=[])
        store.events.emit("agent_paused" if command == "pause_agent" else "agent_resumed", agent=a["name"], by=author)
        return {"ok": True, "paused": command == "pause_agent"}
    if command == "restart_agent":
        store.set_agent_state(a["name"], restart_requested=True)
        return {"ok": True, "message": "the agent's session will be restarted at the next tick"}
    if command == "reset_agent":
        ag = all_agents(project).get(a["name"])
        if ag is None or not ag.resident:
            raise StoreError(f"{a['name']!r} is not a resident agent; only a long-lived session can be reset")
        store.set_agent_state(a["name"], reset_requested=True, reset_by=author)
        return {"ok": True, "message": "the agent's session will start over at the next tick; its transcript so far is kept as an archived run"}
    if command in ("run_agent", "message_agent"):
        teams, ags = all_teams(project), all_agents(project)
        why = may_message(teams, ags, role or "supervisor", a["name"])
        if why:
            raise StoreError(why)
        target = ags[a["name"]]
        text = (a.get("brief") or a.get("note") or "").strip()
        if not text:
            raise StoreError("say what you want done")
        if command == "run_agent" and target.kind != "ad_hoc":
            raise StoreError(f"{target.name} is {target.kind}; use message_agent to add to its next wake-up, run_agent is for ad_hoc agents")
        if command == "message_agent" and target.kind == "ad_hoc":
            raise StoreError(f"{target.name} is ad_hoc: it has no next wake-up to add a note to. Use run_agent with a brief.")
        store.add_wakeup(target.name, f"{'brief' if command == 'run_agent' else 'message'} from {author}: {text}")
        store.events.emit("agent_messaged", agent=target.name, by=author)
        return {"ok": True, "message": f"{target.name} will wake at the next tick with your {'brief' if command == 'run_agent' else 'message'}"}

    # -- teams ----------------------------------------------------------------------------
    if command == "list_teams":
        return {"ok": True, "teams": [team_view(project, store, t) for t in all_teams(project).values()]}
    if command == "get_team":
        t = all_teams(project).get(a["name"])
        if t is None:
            raise StoreError(f"no team {a['name']!r}")
        return {"ok": True, **team_definition(project, store, t)}
    if command in ("create_team", "update_team"):
        if command == "update_team":
            t = os_.get_team(a["name"])
            for k in ("description", "lead", "members", "tracked", "access", "capacity", "emits", "probes"):
                if k in a and a[k] is not None:
                    setattr(t, k, a[k])
            resp = a["responsibilities"] if a.get("responsibilities") else os_.responsibilities(t.name)
        else:
            if a["name"] in all_teams(project):
                raise StoreError(f"team {a['name']!r} exists; use update_team")
            t = TeamDef(name=a["name"], description=a.get("description", ""), lead=a.get("lead") or "",
                        members=list(a.get("members") or []), tracked=bool(a.get("tracked")),
                        access=list(a.get("access") or []), capacity=int(a.get("capacity") or 1), created_by=author,
                        emits=list(a.get("emits") or []), probes=list(a.get("probes") or []))
            resp = a.get("responsibilities") or ""
            if len(resp.strip()) < 20:
                raise StoreError("responsibilities are required: the standing conditions this team keeps true, as a numbered list")
        os_.save_team(t, resp)
        store.events.emit("team_changed" if command == "update_team" else "team_created", team=t.name, by=author)
        if t.lead:
            store.add_wakeup(t.lead, f"team {t.name} was {'updated' if command == 'update_team' else 'created'}; re-read its responsibilities")
        return {"ok": True, "team": t.to_dict(), "storage": os_.last_commit_note}
    if command == "emit_event":
        teams, ags = all_teams(project), all_agents(project)
        etype = str(a.get("type") or "").strip()
        if not etype or etype in EVENT_TYPES_SET:
            raise StoreError("type must be a new lowercase_name declared by your team's `emits`; Orchestra's own types cannot be raised")
        emitter = role or "supervisor"
        if emitter != "supervisor":
            ag = ags.get(emitter)
            allowed = sorted({e for t in team_chain(teams, ag.team if ag else None) for e in t.emits})
            if etype not in allowed:
                raise StoreError(f"{emitter} may not emit {etype!r}; its team chain declares {allowed or 'nothing'}. "
                                 "Ask the Supervisor to add it to the team's `emits`.")
        st = store.agent_state(emitter)
        depth = int(st.get("turn_depth", 0)) + 1
        if depth > EMIT_DEPTH_LIMIT:
            raise StoreError(f"not emitted: this turn is already {depth - 1} events deep in a cause chain "
                             f"(limit {EMIT_DEPTH_LIMIT}). Escalate or write a note instead.")
        now_t = time.time()
        recent = [x for x in st.get("emits", []) if now_t - x < 3600]
        if len(recent) >= EMIT_RATE_LIMIT:
            raise StoreError(f"not emitted: {emitter} has raised {len(recent)} events in the last hour (limit {EMIT_RATE_LIMIT})")
        store.set_agent_state(emitter, emits=[*recent, now_t])
        subject, note = (a.get("subject") or "").strip()[:120], (a.get("note") or "").strip()[:500]
        ev = store.events.emit(etype, by=emitter, subject=subject or None, note=note or None, depth=depth,
                               team=(ags[emitter].team if emitter in ags else None))
        listeners = [x.name for x in ags.values() if x.matches_event(ev) and x.name != emitter]
        return {"ok": True, "event": ev, "wakes": listeners}
    if command == "delete_team":
        t = all_teams(project).get(a["name"])
        if t is None or t.builtin:
            raise StoreError(f"no custom team {a['name']!r}")
        left = members_of(t, all_agents(project))
        if left:
            raise StoreError(f"team {t.name!r} still has agents {left}; move or delete them first")
        os_.delete_team(t.name)
        store.events.emit("team_deleted", team=t.name, by=author)
        return {"ok": True, "storage": os_.last_commit_note}
    raise StoreError(f"unknown command {command}")


# -- views ---------------------------------------------------------------------------------

def role_definition(project: Project, role: RoleDef) -> dict:
    """What makes a role that role: its prompt, where it comes from, the tools behind its groups."""
    override = project.config.prompt_overrides.get(role.name)
    if role.builtin:
        source = str(project.repo_root / override) if override else str(agents.PROMPTS_DIR / f"{role.name}.md")
    else:
        source = str(project.repo_root / ".orchestra" / "roles" / f"{role.name}.md")
    return {"prompt": agents.role_prompt(project, role.name), "prompt_source": source,
            "tool_names": {g: [t.name for t in tools_for([g])] for g in role.tools}}


def agent_runs(store: LocalStore, ag: AgentDef) -> list[dict]:
    """Every session this agent has run under, newest first. A resident agent has one; an
    ephemeral agent has one per run, under a timestamped id, and they are all kept."""
    runs = [s for s in [*store.sessions(), *store.runtime().get("archived_sessions", {}).values()]
            if s.get("kind") == ag.name or s.get("id") == ag.session_id]
    runs.sort(key=lambda s: s.get("started_at") or 0, reverse=True)
    return [{k: s.get(k) for k in ("id", "status", "started_at", "settled_at", "last_text", "session_file", "harness")} for s in runs]


def agent_view(project: Project, store: LocalStore, ag: AgentDef, teams: dict | None = None) -> dict:
    st = store.agent_state(ag.name)
    teams = teams if teams is not None else all_teams(project)
    runs = agent_runs(store, ag)
    return {**ag.to_dict(), "builtin": ag.builtin, "source": ag.source, "session_id": ag.session_id,
            "triggers": list(ag.triggers), "emits": list(ag.emits),
            "last_run_id": runs[0]["id"] if runs else ag.session_id, "run_count": len(runs),
            "team_exists": ag.team is None or ag.team in teams,   # an agent may be created before its team (the guide's order)
            "wake_kind": "orchestra" if ag.builtin else "events",   # built-ins: the loop calls them; custom: they subscribe
            "resident": ag.resident, "paused": bool(st.get("paused")), "deleted": bool(st.get("deleted")),
            "crash_loop": st.get("crash_loop") if st.get("paused") else None,
            "last_woken": st.get("last_woken"), "next_scheduled_at": st.get("next_scheduled_at"),
            "every_seconds": parse_interval(ag.every) if ag.every else None}


def agent_definition(project: Project, store: LocalStore, ag: AgentDef) -> dict:
    roles, teams = all_roles(project), all_teams(project)
    tools = effective_tools(ag, roles, teams)
    return {"agent": agent_view(project, store, ag), "state": store.agent_state(ag.name), "runs": agent_runs(store, ag)[:30],
            "roles": [{**roles[r].to_dict(), "builtin": roles[r].builtin} for r in ag.roles if r in roles],
            "team_chain": [t.name for t in team_chain(teams, ag.team)],
            "leads": [t.name for t in teams.values() if t.lead == ag.name],
            "tool_groups": list(tools), "tool_names": {g: [t.name for t in tools_for([g])] for g in tools},
            "prompt": agents.agent_prompt(project, ag, roles, teams)}


def team_view(project: Project, store: LocalStore, t: TeamDef) -> dict:
    ags = all_agents(project)
    return {**t.to_dict(), "builtin": t.builtin, "parent": t.parent, "space": t.space,
            "agents": [agent_view(project, store, ags[n]) for n in members_of(t, ags)],
            "sub_teams": [x.name for x in all_teams(project).values() if x.parent == t.name]}


def team_definition(project: Project, store: LocalStore, t: TeamDef) -> dict:
    v = team_view(project, store, t)
    resp = "" if t.builtin else org_store(project).responsibilities(t.name)
    chain = team_chain(all_teams(project), t.parent)
    return {"team": v, "responsibilities": resp,
            "inherited": [{"team": x.name, "responsibilities": org_store(project).responsibilities(x.name) if not x.builtin else ""}
                          for x in chain],
            "space_dir": str(t.space_dir(project)), "tools_dir": str(t.space_dir(project) / "tools")}
