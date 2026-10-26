"""Agents and teams as data, behind a store. SPEC.md section 4.3 and
docs/proposals/teams-and-roles.md (Stage 2).

Three things, kept apart on purpose:

- a **role** (``orchestra/roles.py``) describes a capability, like a job description. It
  has no runtime: it cannot be scheduled, hold a session, or receive a message;
- an **agent** is an instance of one or more roles placed in a team. It is what runs: it
  has a kind and a session policy, and it is what gets paused, restarted, messaged, and
  shown on the Dashboard. Three kinds: ``continuous`` is a resident process that is always
  up and is woken by events (like the Coordinator); ``scheduled`` runs on an interval or at
  a daily time, with a resident or an ephemeral session; ``ad_hoc`` gets a fresh session
  per invocation, and an invocation is a brief from its lead or a subscribed event;
- a **team** has exactly one lead agent, members, standing responsibilities, access
  grants, a space in the repository, and optionally a parent team.

Built-in agents and teams are declared here from the built-in roles. Custom ones live in
the repository under ``.orchestra/agents/`` and ``.orchestra/teams/``; a team's working
space is ``.orchestra/spaces/<team>/``. Stage 1 custom roles that carry their own wake
rules are exposed as implicit agents of the same name, so nothing created before teams
existed stops running."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .agents.roles import ROLES as BUILTIN_ROLES
from .config import Project
from .model import now
from .roles import (
    EVENT_TYPES,
    TOOL_GROUPS,
    RoleError,
    all_roles,
    commit_orchestra_files,
    parse_interval,
)

AGENT_KINDS = ("continuous", "scheduled", "ad_hoc")
ACCESS_MODES = ("read", "write")
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class OrgError(ValueError):
    pass


_CUSTOM_TYPE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def event_matches(wake_on: list[str], ev: dict) -> bool:
    """`type` matches the event type; `type:x` also needs x to be the transition target,
    the status, the probe name, or the subject of an agent-emitted event."""
    for w in wake_on:
        t, _, suffix = w.partition(":")
        if ev.get("type") != t:
            continue
        if not suffix or suffix in (ev.get("to"), ev.get("status"), ev.get("probe"), ev.get("subject")):
            return True
    return False


def custom_event_types(teams: dict) -> set[str]:
    out: set[str] = set()
    for t in teams.values():
        out.update(getattr(t, "emits", []) or [])
    return out


@dataclass
class AgentDef:
    name: str
    roles: list[str] = field(default_factory=list)
    team: str | None = None
    kind: str = "continuous"              # continuous | scheduled | ad_hoc
    session: str = "resident"             # resident | ephemeral
    wake_on: list[str] = field(default_factory=list)
    every: str | None = None
    at: str | None = None
    description: str = ""
    created_by: str = "supervisor"
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    builtin: bool = False
    source: str = "agent"                 # agent | role (implicit, from a Stage 1 role record)
    triggers: list[str] = field(default_factory=list)   # built-ins: conditions the loop detects that are not events
    emits: list[str] = field(default_factory=list)      # built-ins: event types their actions cause

    @property
    def session_id(self) -> str:
        return self.name.replace("_", "-")

    @property
    def resident(self) -> bool:
        """A long-lived process is kept for this agent between wake-ups."""
        return self.session == "resident" and self.kind != "ad_hoc"

    @property
    def ephemeral(self) -> bool:
        return not self.resident

    def validate(self, custom_types: set[str] | frozenset[str] = frozenset()) -> None:
        if not _NAME.match(self.name):
            raise OrgError("agent name must be lowercase letters, digits, and underscores, 2 to 41 chars")
        if not self.roles:
            raise OrgError("an agent needs at least one role")
        if self.kind not in AGENT_KINDS:
            raise OrgError(f"kind must be one of {list(AGENT_KINDS)}")
        if self.session not in ("resident", "ephemeral"):
            raise OrgError("session must be 'resident' or 'ephemeral'")
        for w in self.wake_on:
            t = w.split(":", 1)[0]
            if t not in EVENT_TYPES and t not in custom_types:
                raise OrgError(f"unknown event type {w!r}; known: {list(EVENT_TYPES)}"
                               + (f"; declared by teams: {sorted(custom_types)}" if custom_types else ""))
        if self.every is not None:
            try:
                parse_interval(self.every)
            except RoleError as e:
                raise OrgError(str(e))
        if self.at is not None and not re.fullmatch(r"\d{2}:\d{2}", self.at):
            raise OrgError("at must be HH:MM")
        if self.kind == "ad_hoc" and (self.every or self.at):
            raise OrgError("an ad_hoc agent runs when briefed or on a wake_on event; it has no `every` or `at`")
        if self.kind == "ad_hoc" and self.session != "ephemeral":
            raise OrgError("an ad_hoc agent gets a fresh session per invocation: session must be 'ephemeral'")
        if self.kind == "continuous" and self.session != "resident":
            raise OrgError("a continuous agent is a process that is always up: session must be 'resident'. "
                           "For a fresh session on each event, use kind 'ad_hoc' with wake_on.")
        if self.kind == "scheduled" and not (self.every or self.at):
            raise OrgError("a scheduled agent needs `every` or `at`")

    def matches_event(self, ev: dict) -> bool:
        """Built-in agents' wake lists describe what the loop does for them (it calls their
        wake-ups directly); only custom agents subscribe to the event log."""
        return not self.builtin and event_matches(self.wake_on, ev)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("builtin", "source", "triggers", "emits"):
            d.pop(k, None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> AgentDef:
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TeamDef:
    name: str                             # "product" or, nested, "product/qa"
    description: str = ""
    lead: str = ""                        # agent name
    members: list[str] = field(default_factory=list)   # agent names, lead excluded
    tracked: bool = False                 # is the space committed with the code?
    access: list[dict] = field(default_factory=list)   # {"path", "access"} or {"tools": [...]}
    capacity: int = 1                     # member sessions that may run at once
    emits: list[str] = field(default_factory=list)     # event types this team's agents may raise with emit_event
    probes: list[dict] = field(default_factory=list)   # {"name", "command", "every", "timeout"?}, run in the space
    created_by: str = "supervisor"
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    builtin: bool = False

    @property
    def parent(self) -> str | None:
        return self.name.rpartition("/")[0] or None

    @property
    def agents(self) -> list[str]:
        return ([self.lead] if self.lead else []) + [m for m in self.members if m != self.lead]

    @property
    def space(self) -> str:
        return f".orchestra/spaces/{self.name}"

    def space_dir(self, project: Project) -> Path:
        return project.repo_root / self.space

    @property
    def tool_groups(self) -> list[str]:
        out: list[str] = []
        for g in self.access:
            for t in g.get("tools") or []:
                if t not in out:
                    out.append(t)
        return out

    @property
    def paths(self) -> list[dict]:
        return [g for g in self.access if g.get("path")]

    def validate(self) -> None:
        for seg in self.name.split("/"):
            if not _NAME.match(seg):
                raise OrgError("team name segments must be lowercase letters, digits, and underscores; nest with '/'")
        if not self.lead:
            raise OrgError("a team needs a lead agent")
        if self.lead in self.members:
            raise OrgError("the lead is not also a member")
        if self.capacity < 0:
            raise OrgError("capacity must be 0 or more")
        for e in self.emits:
            if not _CUSTOM_TYPE.match(e) or e in EVENT_TYPES:
                raise OrgError(f"emits entry {e!r} must be a new lowercase_name, not one of Orchestra's own event types")
        for pr in self.probes:
            if not isinstance(pr, dict) or not _NAME.match(str(pr.get("name", ""))) or not pr.get("command"):
                raise OrgError("each probe is {name, command, every} with a lowercase_name")
            try:
                parse_interval(str(pr.get("every", "5m")))
            except RoleError as e:
                raise OrgError(f"probe {pr.get('name')}: {e}")
        for g in self.access:
            if "tools" in g:
                bad = [t for t in g["tools"] if t not in TOOL_GROUPS]
                if bad:
                    raise OrgError(f"unknown tool groups {bad}; known: {list(TOOL_GROUPS)}")
            elif "path" in g:
                if g.get("access") not in ACCESS_MODES:
                    raise OrgError("a path grant needs access 'read' or 'write'")
                if str(g["path"]).startswith("/") or ".." in str(g["path"]).split("/"):
                    raise OrgError("path grants are relative to the repository and may not climb out of it")
            else:
                raise OrgError("each access entry is {path, access} or {tools}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("builtin", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> TeamDef:
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


# -- store ------------------------------------------------------------------------------

class RepoOrgStore:
    """Agents and teams in the target repository: ``.orchestra/agents/<name>.json``,
    ``.orchestra/teams/<team>.json`` with ``<team>.md`` for the responsibilities, and
    ``.orchestra/spaces/<team>/`` for the team's working space. Writes are committed to
    the base branch when the checkout allows it, like roles."""

    def __init__(self, project: Project):
        self.project = project
        self.root = project.repo_root / ".orchestra"
        self.agents_dir = self.root / "agents"
        self.teams_dir = self.root / "teams"
        self.spaces_dir = self.root / "spaces"
        self.last_commit_note = ""

    # agents
    def list_agents(self) -> list[AgentDef]:
        out = []
        for p in sorted(self.agents_dir.glob("*.json")):
            try:
                out.append(AgentDef.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def get_agent(self, name: str) -> AgentDef:
        p = self.agents_dir / f"{name}.json"
        if not p.exists():
            raise OrgError(f"no agent {name!r}")
        return AgentDef.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def save_agent(self, agent: AgentDef, custom_types: set[str] | frozenset[str] = frozenset()) -> AgentDef:
        agent.validate(custom_types)
        if agent.name in BUILTIN_ROLES:
            raise OrgError(f"{agent.name!r} is a built-in agent; choose another name")
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        agent.updated_at = now()
        (self.agents_dir / f"{agent.name}.json").write_text(json.dumps(agent.to_dict(), indent=2) + "\n", encoding="utf-8")
        self.last_commit_note = commit_orchestra_files(self.project, f"tasking: agent {agent.name}")
        return agent

    def delete_agent(self, name: str) -> None:
        p = self.agents_dir / f"{name}.json"
        if not p.exists():
            raise OrgError(f"no agent {name!r}")
        p.unlink()
        self.last_commit_note = commit_orchestra_files(self.project, f"tasking: remove agent {name}")

    # teams
    def _team_paths(self, name: str) -> tuple[Path, Path]:
        return self.teams_dir / f"{name}.json", self.teams_dir / f"{name}.md"

    def list_teams(self) -> list[TeamDef]:
        out = []
        for p in sorted(self.teams_dir.rglob("*.json")):
            try:
                out.append(TeamDef.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def get_team(self, name: str) -> TeamDef:
        p, _ = self._team_paths(name)
        if not p.exists():
            raise OrgError(f"no team {name!r}")
        return TeamDef.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def responsibilities(self, name: str) -> str:
        _, p = self._team_paths(name)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def save_team(self, team: TeamDef, responsibilities: str) -> TeamDef:
        team.validate()
        if team.name in BUILTIN_TEAMS:
            raise OrgError(f"{team.name!r} is a built-in team; choose another name")
        if team.parent and not (self._team_paths(team.parent)[0].exists() or team.parent in BUILTIN_TEAMS):
            raise OrgError(f"parent team {team.parent!r} does not exist")
        pj, pm = self._team_paths(team.name)
        pj.parent.mkdir(parents=True, exist_ok=True)
        team.updated_at = now()
        pj.write_text(json.dumps(team.to_dict(), indent=2) + "\n", encoding="utf-8")
        pm.write_text(responsibilities.rstrip() + "\n", encoding="utf-8")
        self.ensure_space(team)
        self.last_commit_note = commit_orchestra_files(self.project, f"tasking: team {team.name}")
        return team

    def delete_team(self, name: str) -> None:
        pj, pm = self._team_paths(name)
        if not pj.exists():
            raise OrgError(f"no team {name!r}")
        if any(t.parent == name for t in self.list_teams()):
            raise OrgError(f"team {name!r} has sub-teams; delete or move them first")
        for p in (pj, pm):
            if p.exists():
                p.unlink()
        self.last_commit_note = commit_orchestra_files(self.project, f"tasking: remove team {name}")

    def ensure_space(self, team: TeamDef) -> Path:
        """The team's working space: created with a tools/ directory, and listed in
        .orchestra/.gitignore unless the team asked for it to be tracked. The space is
        never deleted by Orchestra: it is the team's, and it may hold work."""
        space = team.space_dir(self.project)
        (space / "tools").mkdir(parents=True, exist_ok=True)
        gi = self.root / ".gitignore"
        line = f"spaces/{team.name}/"
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        if team.tracked:
            lines = [ln for ln in lines if ln.strip() != line]
        elif line not in lines:
            lines.append(line)
        gi.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return space


def org_store(project: Project) -> RepoOrgStore:
    return RepoOrgStore(project)


# -- built-ins and the merged view --------------------------------------------------------

def built_in_agents() -> dict[str, AgentDef]:
    out: dict[str, AgentDef] = {}
    for b in BUILTIN_ROLES.values():
        team = {"supervisor": None, "coordinator": "engineering", "worker": "engineering",
                "reviewer": "engineering"}.get(b.name)
        kind = "ad_hoc" if not b.resident else "scheduled" if b.every else "continuous"
        out[b.name] = AgentDef(name=b.name, roles=[b.name], team=team, kind=kind,
                               session="ephemeral" if not b.resident else "resident", wake_on=list(b.wake_on),
                               every=(f"{int(b.every // 60)}m" if b.every else None), description=b.description,
                               created_by="orchestra", builtin=True, triggers=list(b.triggers), emits=list(b.emits))
    return out


BUILTIN_TEAMS: dict[str, TeamDef] = {
    "engineering": TeamDef(name="engineering", lead="coordinator", members=["worker", "reviewer"], builtin=True,
                           created_by="orchestra",
                           description="Turns tickets into finished work: the Coordinator keeps the plan executable, "
                                       "workers do the tickets, reviewers review them."),
}


def all_teams(project: Project) -> dict[str, TeamDef]:
    out = dict(BUILTIN_TEAMS)
    for t in org_store(project).list_teams():
        out[t.name] = t
    return out


def all_agents(project: Project) -> dict[str, AgentDef]:
    """Built-in agents, implicit agents for Stage 1 roles that carry wake rules and have
    no explicit agent, and the project's own agents, by name."""
    out = built_in_agents()
    explicit = {a.name: a for a in org_store(project).list_agents()}
    for a in explicit.values():
        # A record from before the kinds were pinned down: "continuous" with an ephemeral
        # session is an ad hoc agent on events. Read it as that; update_agent fixes the file.
        if a.kind == "continuous" and a.session == "ephemeral":
            a.kind = "ad_hoc"
    for r in all_roles(project).values():
        if r.builtin or r.name in explicit or not (r.wake_on or r.every or r.at):
            continue   # a role created under Stage 2 carries no wake rules: it runs only through an agent
        kind = "scheduled" if (r.every or r.at) else "continuous"
        out[r.name] = AgentDef(name=r.name, roles=[r.name], kind=kind, session=r.session, wake_on=list(r.wake_on),
                               every=r.every, at=r.at, description=r.description, created_by=r.created_by,
                               created_at=r.created_at, updated_at=r.updated_at, source="role")
    out.update(explicit)
    # A team's record is authoritative for who is in it: an agent it names as lead or member
    # belongs to it even when the agent's own record (or a Stage 1 role record, which has no
    # team field) says nothing, or says something older.
    for t in all_teams(project).values():
        for name in t.agents:
            if name in out and not out[name].builtin and out[name].team != t.name:
                out[name].team = t.name
    return out


def members_of(team: TeamDef, agents: dict[str, AgentDef]) -> list[str]:
    """The team's agents: those its record names, then any whose own record names the team
    (an agent is allowed to be created before the team lists it)."""
    names = [n for n in team.agents if n in agents]
    names += [a.name for a in agents.values() if a.team == team.name and a.name not in names]
    return names


def team_chain(teams: dict[str, TeamDef], name: str | None) -> list[TeamDef]:
    """The team and its ancestors, nearest first."""
    out: list[TeamDef] = []
    while name and name in teams:
        out.append(teams[name])
        name = teams[name].parent
    return out


def teams_led_by(teams: dict[str, TeamDef], agent: str) -> list[TeamDef]:
    return [t for t in teams.values() if t.lead == agent]


def effective_tools(agent: AgentDef, roles: dict, teams: dict[str, TeamDef]) -> tuple[str, ...]:
    """Tool groups an agent runs with: its roles' groups, its team chain's granted groups,
    and `team` for every agent in a team, so a member can reach its lead with
    message_agent and read the organisation. Who may message whom is enforced in the
    API by the tree, not by the grant."""
    out: list[str] = []
    for r in agent.roles:
        for g in (roles[r].tools if r in roles else ()):
            if g not in out:
                out.append(g)
    for t in team_chain(teams, agent.team):
        for g in t.tool_groups:
            if g not in out:
                out.append(g)
    if (agent.team in teams or teams_led_by(teams, agent.name)) and "team" not in out:
        out.append("team")
    return tuple(out)


def tools_path(project: Project, teams: dict[str, TeamDef], agent: AgentDef) -> list[Path]:
    """The team chain's tools/ directories, nearest first, for the agent's PATH."""
    return [t.space_dir(project) / "tools" for t in team_chain(teams, agent.team)]


def may_message(teams: dict[str, TeamDef], agents: dict[str, AgentDef], sender: str, target: str) -> str | None:
    """The tree: a lead may message its team's agents and its sub-teams' leads, an agent
    may message its own lead, the Supervisor may message anyone. Returns why not, or None."""
    if sender == "supervisor":
        return None
    if target not in agents:
        return f"no agent {target!r}"
    t_team = agents[target].team
    s_team = agents.get(sender).team if sender in agents else None
    led = {t.name for t in teams_led_by(teams, sender)}
    if t_team in led:
        return None                                   # my member
    if t_team and teams[t_team].parent in led and teams[t_team].lead == target:
        return None                                   # the lead of my sub-team
    if s_team and teams.get(s_team) and teams[s_team].lead == target:
        return None                                   # my own lead
    for t in teams_led_by(teams, sender):
        if t.parent and t.parent in teams and teams[t.parent].lead == target:
            return None                               # the lead of the team above mine
    return (f"{sender} may not message {target}: agents talk only to their own lead, leads to their team and "
            f"to the leads of their sub-teams. Escalate to the Supervisor instead.")
