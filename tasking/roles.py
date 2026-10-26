"""Roles as data, behind a store interface. SPEC.md section 4.3 and the Stage 1 proposal.

A role is a JSON record plus a prompt file. Built-in roles live in
``orchestra/agents/roles.py``; custom roles come from a RoleStore. The default store is
the project repository under ``.orchestra/roles/``, versioned with the code. Other
stores (an external git repository, an object store) implement the same interface."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .agents.roles import ROLES as BUILTIN, Role as BuiltinRole
from .config import Project
from .model import now

TOOL_GROUPS = ("tickets", "context", "pipeline", "capacity", "escalate", "events", "roles", "team")
EVENT_TYPES = (
    "ticket_created", "ticket_updated", "ticket_transition", "note_added",
    "milestone_created", "milestone_updated", "milestone_closed",
    "gate_started", "gate_landed", "gate_rejected", "gate_conflict", "gate_error", "gate_base_broken",
    "staging_deployed", "staging_failed", "staging_restarted",
    "pipeline_paused", "pipeline_resumed", "pickup_paused", "pickup_resumed", "capacity_changed",
    "agent_said", "agent_killed", "context_updated", "loop_started", "loop_stopped",
    "session_started", "session_settled",
    "role_created", "role_changed", "role_deleted", "agent_created", "agent_changed", "agent_deleted",
    "agent_paused", "agent_resumed", "agent_messaged", "team_created", "team_changed", "team_deleted",
    "probe_failed", "probe_recovered", "turn_timed_out", "turn_error", "turn_interrupted", "agent_crashed", "agent_crash_loop", "agent_reset", "agent_compacted",
)
# Teams may declare further event types (`emits`) that their agents raise with `emit_event`;
# those are validated against the teams, not this list.
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class RoleError(ValueError):
    pass


@dataclass
class RoleDef:
    """A custom role. Built-in roles are expressed in these terms too (see `all_roles`)."""

    name: str
    description: str = ""
    tools: list[str] = field(default_factory=lambda: ["tickets", "escalate"])
    session: str = "resident"            # resident | ephemeral
    wake_on: list[str] = field(default_factory=list)   # event types, optionally "ticket_transition:DONE"
    every: str | None = None             # interval like "30m", "6h", "1d"
    at: str | None = None                # daily time "HH:MM" (local)
    created_by: str = "supervisor"
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    builtin: bool = False

    @property
    def session_id(self) -> str:
        return self.name.replace("_", "-")

    @property
    def resident(self) -> bool:
        return self.session == "resident"

    def validate(self) -> None:
        if not _NAME.match(self.name):
            raise RoleError("name must be lowercase letters, digits, and underscores, 2 to 41 chars")
        bad = [t for t in self.tools if t not in TOOL_GROUPS]
        if bad:
            raise RoleError(f"unknown tool groups {bad}; known: {list(TOOL_GROUPS)}")
        if self.session not in ("resident", "ephemeral"):
            raise RoleError("session must be 'resident' or 'ephemeral'")
        for w in self.wake_on:
            if w.split(":", 1)[0] not in EVENT_TYPES:
                raise RoleError(f"unknown event type {w!r}; known: {list(EVENT_TYPES)}")
        if self.every is not None:
            parse_interval(self.every)
        if self.at is not None and not re.fullmatch(r"\d{2}:\d{2}", self.at):
            raise RoleError("at must be HH:MM")

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("builtin", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> RoleDef:
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})

    def matches_event(self, ev: dict) -> bool:
        for w in self.wake_on:
            t, _, suffix = w.partition(":")
            if ev.get("type") != t:
                continue
            if not suffix or ev.get("to") == suffix or ev.get("status") == suffix:
                return True
        return False


def parse_interval(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+)\s*(m|h|d)\s*", text)
    if not m:
        raise RoleError("every must look like 30m, 6h, or 1d")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def due_at(at: str, last: float | None, now_ts: float) -> bool:
    """True if the daily `at` time has passed today and has not fired since."""
    hh, mm = (int(x) for x in at.split(":"))
    today = datetime.fromtimestamp(now_ts).replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
    return now_ts >= today and (last is None or last < today)


class RoleStore(Protocol):
    def list(self) -> list[RoleDef]: ...
    def get(self, name: str) -> RoleDef: ...
    def prompt(self, name: str) -> str: ...
    def save(self, role: RoleDef, prompt: str) -> RoleDef: ...
    def delete(self, name: str) -> None: ...


class RepoRoleStore:
    """Roles in the target repository: ``.orchestra/roles/<name>.json`` and ``<name>.md``.

    Writes are committed to the base branch when the checkout is on it and otherwise
    clean, so a role change never leaves the tree dirty for the merge gate. If the
    checkout is elsewhere or dirty, the files are written and left for the human to
    commit; `last_commit_note` says which happened."""

    def __init__(self, project: Project):
        self.project = project
        self.dir = project.repo_root / ".orchestra" / "roles"
        self.last_commit_note = ""

    def list(self) -> list[RoleDef]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(RoleDef.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def get(self, name: str) -> RoleDef:
        p = self.dir / f"{name}.json"
        if not p.exists():
            raise RoleError(f"no role {name!r}")
        return RoleDef.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def prompt(self, name: str) -> str:
        p = self.dir / f"{name}.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def save(self, role: RoleDef, prompt: str) -> RoleDef:
        role.validate()
        if role.name in BUILTIN:
            raise RoleError(f"{role.name!r} is a built-in role; choose another name")
        self.dir.mkdir(parents=True, exist_ok=True)
        role.updated_at = now()
        (self.dir / f"{role.name}.json").write_text(json.dumps(role.to_dict(), indent=2) + "\n", encoding="utf-8")
        (self.dir / f"{role.name}.md").write_text(prompt.rstrip() + "\n", encoding="utf-8")
        self._commit(f"tasking: role {role.name}")
        return role

    def delete(self, name: str) -> None:
        if name in BUILTIN:
            raise RoleError(f"{name!r} is built in and cannot be deleted")
        found = False
        for suffix in (".json", ".md"):
            p = self.dir / f"{name}{suffix}"
            if p.exists():
                p.unlink()
                found = True
        if not found:
            raise RoleError(f"no role {name!r}")
        self._commit(f"tasking: remove role {name}")

    def _commit(self, message: str) -> None:
        self.last_commit_note = commit_orchestra_files(self.project, message)


def commit_orchestra_files(project: Project, message: str) -> str:
    """Commit everything under .orchestra/ to the base branch when the checkout is on it
    and otherwise clean, so an Orchestra-owned file never leaves the tree dirty for the
    merge gate. Returns a note saying what happened. Shared by the role and org stores."""
    root = project.repo_root
    base = project.config.base_branch
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)
    branch = run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    dirty = [ln for ln in run("status", "--porcelain", "--untracked-files=all").stdout.splitlines() if ln.strip()
             and not ln[3:].strip().strip('"').startswith(".orchestra/")]
    if branch != base or dirty:
        return (f"files written under .orchestra/ but not committed: checkout is on {branch!r}"
                + (" with other changes" if dirty else "") + f"; commit them on {base} when convenient")
    run("add", "-A", ".orchestra")
    p = run("commit", "-q", "-m", message)
    return "committed to " + base if p.returncode == 0 else "nothing to commit"


def role_store(project: Project) -> RoleStore:
    """The configured role store. Only the repository store exists today."""
    return RepoRoleStore(project)


def all_roles(project: Project) -> dict[str, RoleDef]:
    """Built-in roles plus the project's custom roles, by name."""
    out: dict[str, RoleDef] = {}
    for b in BUILTIN.values():
        if not b.resident:
            continue
        out[b.name] = RoleDef(name=b.name, description=b.description, tools=list(b.tools), session="resident",
                              wake_on=list(b.wake_on), every=(f"{int(b.every // 60)}m" if b.every else None), builtin=True)
    for r in role_store(project).list():
        out[r.name] = r
    return out
