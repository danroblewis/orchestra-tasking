"""The task system's settings and where it keeps its state.

The settings are sections of the project's configuration that Orchestra itself ignores
([project], [tasks] with [[tasks.recurring]], [build], [acceptance], [gate], [staging], [[probes]]). They are read only
through Orchestra's objects: ``project.orchestra.config.raw``, which is ``orchestra.toml`` with the
owner's private overlay merged over it, reloaded by ``project.orchestra.reload_config()``. Nothing
here opens ``orchestra.toml`` (Orchestra's AGENTS.md rule 10); ``init_project`` adds the task
system's sections through ``project.config_edit``. The project is Orchestra's
(``orchestra.config.Project``: the key, the bus, the metrics store), held here with the task
system's own config; its state lives in ``~/.orchestrator/<key>/tasks/``, beside Orchestra's."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
from orchestra.config import ConfigError, parse_duration
from orchestra.config import Project as OrchestraProject
from orchestra.config import load_project as load_orchestra_project

__all__ = ["Config", "ConfigError", "External", "Project", "TestSpec", "init_project", "load_project", "parse_duration"]


@dataclass
class TestSpec:
    name: str
    command: str
    timeout: float = 900.0


@dataclass
class External:
    """``[gate.external]``: how a ticket follows its pull request (tasking/external.py). Which
    labels mean what is the owner's, not the bridge's: the bridge only names labels."""
    repository: str | None = None      # owner/repo: only this repository's pull requests (any, when unset)
    failing_labels: tuple[str, ...] = ()   # a label added that re-engages the worker (SAIL-failed)
    passing_labels: tuple[str, ...] = ()   # a label added that is noted; only a merge closes the ticket
    line_comments: bool = True         # review comments on lines (and a commenting review with some) re-engage
    conversation_comments: bool = False    # comments on the conversation re-engage
    failing_checks: bool = True        # a check that ends failure, timed_out or error re-engages
    settle: float = 15.0               # feedback is gathered until none came for this long, then the worker is re-engaged once

    @classmethod
    def from_raw(cls, x: dict[str, Any]) -> External:
        names = lambda k: tuple(str(v) for v in (x.get(k) or ()))
        return cls(repository=x.get("repository") or None, failing_labels=names("failing_labels"), passing_labels=names("passing_labels"),
                   line_comments=bool(x.get("line_comments", True)), conversation_comments=bool(x.get("conversation_comments", False)),
                   failing_checks=bool(x.get("failing_checks", True)), settle=parse_duration(x.get("settle"), 15.0))


@dataclass
class Config:
    base_branch: str = "main"
    ticket_prefix: str = "ORC"
    work: str = "tasks"          # tasks: a scratch folder per ticket, no merging | code: a branch per ticket, the merge gate
    push: bool = False
    workers: int = 1             # tickets offered to workers at once
    reviewers: int = 1
    tick: float = 3.0            # how often the scheduler looks at the queue and the bus, in seconds
    return_timeout: float = 60.0  # rejected work waits this long for the worker it is addressed to, then the queue
    review_rounds_before_escalation: int = 3
    aging_days_per_level: float = 2.0
    reuse_worktrees: bool = False      # code mode: keep a pool of worktrees and reset one per turn (tasking/pool.py)
    worktree_pool: int = 3             # how many worktrees the pool may hold, the gate's included; never more
    worktree_clean_ignored: bool = False   # a reset also removes ignored files (git clean -x): no warm caches
    build_command: str | None = None
    build_artifact: str | None = None
    build_timeout: float = 600.0
    acceptance_start: str | None = None
    acceptance_ready: str | None = None
    acceptance_ready_timeout: float = 60.0
    gate_timeout: float = 900.0
    gate_mode: str = "land"      # land: the gate merges and lands | external: it hands the branch off for a pull request
    external: External = field(default_factory=External)
    unit: list[TestSpec] = field(default_factory=list)
    acceptance: list[TestSpec] = field(default_factory=list)
    staging_start: str | None = None
    staging_port: int = 8001
    staging_health: str = "/"
    staging_health_timeout: float = 60.0
    staging_stop: str | None = None
    tunnel_name: str | None = None
    tunnel_hostname: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def staging_configured(self) -> bool:
        return bool(self.staging_start)

    @property
    def external_review(self) -> bool:
        """The gate hands approved branches off for a pull request instead of landing them: code
        mode with ``[gate] mode = "external"`` (in tasks mode there is no branch to hand off)."""
        return self.work == "code" and self.gate_mode == "external"

    @classmethod
    def from_toml(cls, text: str) -> Config:
        """The settings in a TOML text (tests; the project's come from ``from_raw``)."""
        return cls.from_raw(tomllib.loads(text))

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Config:
        """The settings in a parsed configuration: Orchestra's ``project.config.raw``."""
        p, b, acc, g, st = (raw.get(k, {}) for k in ("project", "build", "acceptance", "gate", "staging"))
        a = raw.get("tasks") or {}
        tun = st.get("tunnel", {}) or {}
        gt = parse_duration(g.get("timeout"), 900.0)

        def specs(items: list[dict]) -> list[TestSpec]:
            return [TestSpec(name=i["name"], command=i["command"], timeout=parse_duration(i.get("timeout"), gt)) for i in items]

        return cls(
            base_branch=p.get("base_branch", "main"),
            ticket_prefix=p.get("ticket_prefix", "ORC"),
            work=p.get("work", "tasks"),
            push=bool(p.get("push", False)),
            workers=int(a.get("workers", 1)),
            reviewers=int(a.get("reviewers", 1)),
            tick=parse_duration(a.get("tick"), 3.0),
            return_timeout=parse_duration(a.get("return_timeout"), 60.0),
            review_rounds_before_escalation=int(a.get("review_rounds_before_escalation", 3)),
            aging_days_per_level=float(a.get("aging_days_per_level", 2.0)),
            reuse_worktrees=bool(a.get("reuse_worktrees", False)),
            worktree_pool=max(1, int(a.get("worktree_pool", int(a.get("workers", 1)) + int(a.get("reviewers", 1)) + 1))),
            worktree_clean_ignored=bool(a.get("worktree_clean_ignored", False)),
            build_command=b.get("command"),
            build_artifact=b.get("artifact"),
            build_timeout=parse_duration(b.get("timeout"), 600.0),
            acceptance_start=acc.get("start"),
            acceptance_ready=acc.get("ready"),
            acceptance_ready_timeout=parse_duration(acc.get("ready_timeout"), 60.0),
            gate_timeout=gt,
            gate_mode=str(g.get("mode", "land")),
            external=External.from_raw(g.get("external") or {}),
            unit=specs(g.get("unit", [])),
            acceptance=specs(g.get("acceptance", [])),
            staging_start=st.get("start"),
            staging_port=int(st.get("port", 8001)),
            staging_health=st.get("health", "/"),
            staging_health_timeout=parse_duration(st.get("health_timeout"), 60.0),
            staging_stop=st.get("stop"),
            tunnel_name=tun.get("name"),
            tunnel_hostname=tun.get("hostname"),
            raw=raw,
        )


STARTER_TOML = """[project]
base_branch = "main"
ticket_prefix = "{prefix}"
work = "tasks"           # tasks: a scratch folder per ticket | code: a git branch per ticket and the merge gate
push = false

[tasks]
workers = 2              # tickets offered to workers at once (whatever fills the role takes them)
reviewers = 1
# tick = "3s"            # how often the scheduler looks at the queue and the bus
# return_timeout = "60s"  # rejected work waits this long for the worker that did it, then goes to any worker
review_rounds_before_escalation = 3
aging_days_per_level = 2        # a ticket gains one priority level per N days of age; 0 disables
# reuse_worktrees = true         # code mode: reuse a pool of worktrees instead of one per ticket
# worktree_pool = 2              # at most this many (default workers + reviewers + 1); a turn waits for a free one
# worktree_clean_ignored = false # true: a reset also removes ignored files (build caches, node_modules)

# [build]
# command = "uv build"
# artifact = "dist/*.whl"

# [acceptance]
# start = "{{python}} -m myapp serve --port {{port}}"   # {{python}}: a venv with {{artifact}} installed
# ready = "http://127.0.0.1:{{port}}/health"

[gate]
timeout = "900s"
# mode = "external"      # code mode: hand approved branches off for a pull request instead of landing them

# [gate.external]        # with mode = "external": the ticket follows its pull request (a bridge publishes github.pr.*)
# repository = "acme/service"
# failing_labels = ["SAIL-failed"]   # re-engage the worker that wrote it
# passing_labels = ["SAIL-passed"]   # noted; only a merge closes the ticket
# line_comments = true               # review comments re-engage
# conversation_comments = false
# failing_checks = true
# settle = "15s"                     # gather a review's comments before re-engaging, once

[[gate.unit]]
name = "unit"
command = "{unit_command}"

# [[gate.acceptance]]
# name = "acceptance"
# command = "uv run pytest tests/acceptance -q --base-url {{url}}"
#
# [[tasks.recurring]]              # a ticket made at each due time (SPEC.md section 7; tasking recurring check)
# name = "weekly-update"
# every = "week"                   # day | week | month, at a local time; or cron = "0 9 * * mon"
# on = "mon"
# at = "09:00"
# timezone = "Europe/London"
# title = "Weekly update, week of {{date}}"
# milestone = "M1"
# if_open = "skip"                 # nothing new while the last one is open
#
# [[probes]]                       # a condition the scheduler checks on an interval; exit 0 = holds.
# name = "staging_fast"            # probe.failed / probe.recovered events fire on the transition,
# command = "scripts/p95_under_1s.sh $STAGING_URL"   # so whatever listens for them wakes once.
# every = "5m"                     # ms, s, m or h (a day is "24h")
# timeout = "60s"
"""


@dataclass
class Project:
    orchestra: OrchestraProject    # the same repository as Orchestra sees it: its key, bus, metrics store and state
    config: Config

    @classmethod
    def from_orchestra(cls, o: OrchestraProject) -> Project:
        return cls(orchestra=o, config=Config.from_raw(o.config.raw))

    @property
    def key(self) -> str:
        return self.orchestra.key

    @property
    def repo_root(self) -> Path:
        return self.orchestra.repo_root

    @property
    def state_dir(self) -> Path:
        return self.orchestra.state_dir / "tasks"

    @property
    def tickets_file(self) -> Path:
        return self.state_dir / "tickets.json"

    @property
    def runtime_file(self) -> Path:
        return self.state_dir / "runtime.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / ".lock"

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

    @property
    def pool_dir(self) -> Path:
        return self.state_dir / "pool"

    @property
    def workspaces_dir(self) -> Path:
        return self.state_dir / "workspaces"

    @property
    def gate_dir(self) -> Path:
        return self.state_dir / "gate"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def staging_dir(self) -> Path:
        return self.state_dir / "staging"

    @property
    def artifacts_dir(self) -> Path:
        return self.state_dir / "artifacts"

    def ensure_dirs(self) -> None:
        for d in (self.state_dir, self.worktrees_dir, self.gate_dir, self.logs_dir,
                  self.staging_dir, self.artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def config_edit(self):
        """Orchestra's way to change the configuration (``orchestra.config_edit``)."""
        return self.orchestra.config_edit

    def reload_config(self) -> None:
        """The task system's own sections, read again through Orchestra, the private overlay merged
        (the scheduler does this every tick)."""
        self.orchestra.reload_config()
        self.config = Config.from_raw(self.orchestra.config.raw)


def load_project(repo: Path | str | None = None) -> Project:
    """The project for ``repo`` (default: ORCHESTRA_REPO, else the cwd), as Orchestra resolves it."""
    return Project.from_orchestra(load_orchestra_project(repo))


def init_project(repo: Path, *, prefix: str | None = None, unit_command: str = "uv run pytest -q") -> Project:
    """Orchestra's starter ``orchestra.toml`` (if there is none) with the task system's sections
    added (if the configuration has no ``[project]``), through Orchestra (``config_edit``, which
    keeps the starter's comments), and the project registered with Orchestra."""
    from orchestra.config import init_project as init_orchestra
    o = init_orchestra(Path(repo))
    if "project" not in o.config.raw:
        pfx = prefix or re.sub(r"[^A-Za-z]", "", o.repo_root.name.upper())[:4] or "ORC"
        starter = tomlkit.parse(STARTER_TOML.format(prefix=pfx, unit_command=unit_command))
        for section in starter:
            if section not in o.config.raw:
                o.config_edit.set(section, starter[section])
    return Project.from_orchestra(o)
