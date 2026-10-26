"""The task system's settings and where it keeps its state.

The settings are sections of the repository's ``orchestra.toml`` that Orchestra itself ignores
([project], [tasks], [build], [acceptance], [gate], [staging], [[probes]]); [events] and
[metrics] are Orchestra's, read here only to reach the same bus and metrics store. State lives
in ``~/.orchestrator/<key>/tasks/``, beside Orchestra's own; the project key is Orchestra's.

Lifted from Orchestra's config.py as it was before the task system was removed (5006caf^)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestra.config import Config as OrchestraConfig
from orchestra.config import ConfigError, find_repo_root, home, parse_duration, project_key
from orchestra.config import Project as OrchestraProject
from orchestra.config import load_project as load_orchestra_project

__all__ = ["Config", "ConfigError", "Project", "TestSpec", "find_repo_root", "home", "init_project",
           "load_project", "parse_duration", "project_key"]


@dataclass
class TestSpec:
    name: str
    command: str
    select: str | None = None
    timeout: float = 900.0


@dataclass
class Config:
    base_branch: str = "main"
    ticket_prefix: str = "ORC"
    work: str = "tasks"          # tasks: a scratch folder per ticket, no merging | code: a branch per ticket, the merge gate
    push: bool = False
    workers: int = 1             # tickets offered to workers at once
    reviewers: int = 1
    review_rounds_before_escalation: int = 3
    aging_days_per_level: float = 2.0
    build_command: str | None = None
    build_artifact: str | None = None
    build_timeout: float = 600.0
    acceptance_start: str | None = None
    acceptance_ready: str | None = None
    acceptance_ready_timeout: float = 60.0
    acceptance_stop: str | None = None
    batch_size: int = 6
    batch_wait: float = 120.0
    gate_timeout: float = 900.0
    unit: list[TestSpec] = field(default_factory=list)
    acceptance: list[TestSpec] = field(default_factory=list)
    staging_start: str | None = None
    staging_port: int = 8001
    staging_health: str = "/"
    staging_health_timeout: float = 60.0
    staging_stop: str | None = None
    tunnel_name: str | None = None
    tunnel_hostname: str | None = None
    events_url: str | None = None      # [events] url: file (default), redis://, amqp://, kafka://
    metrics_url: str | None = None     # [metrics] url: file (default) or http://host:port (VictoriaMetrics)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def staging_configured(self) -> bool:
        return bool(self.staging_start)

    @classmethod
    def from_toml(cls, text: str) -> Config:
        raw = tomllib.loads(text)
        p, b, acc, g, st = (raw.get(k, {}) for k in ("project", "build", "acceptance", "gate", "staging"))
        a = {**(raw.get("agents") or {}), **(raw.get("tasks") or {})}   # [tasks]; before the agents left, these were in [agents]
        tun = st.get("tunnel", {}) or {}
        gt = parse_duration(g.get("timeout"), 900.0)

        def specs(items: list[dict]) -> list[TestSpec]:
            return [
                TestSpec(
                    name=i["name"], command=i["command"], select=i.get("select"),
                    timeout=parse_duration(i.get("timeout"), gt),
                )
                for i in items
            ]

        return cls(
            base_branch=p.get("base_branch", "main"),
            ticket_prefix=p.get("ticket_prefix", "ORC"),
            work=p.get("work", "tasks"),
            push=bool(p.get("push", False)),
            workers=int(a.get("workers", 1)),
            reviewers=int(a.get("reviewers", 1)),
            review_rounds_before_escalation=int(a.get("review_rounds_before_escalation", 3)),
            aging_days_per_level=float(a.get("aging_days_per_level", 2.0)),
            build_command=b.get("command"),
            build_artifact=b.get("artifact"),
            build_timeout=parse_duration(b.get("timeout"), 600.0),
            acceptance_start=acc.get("start"),
            acceptance_ready=acc.get("ready"),
            acceptance_ready_timeout=parse_duration(acc.get("ready_timeout"), 60.0),
            acceptance_stop=acc.get("stop"),
            batch_size=int(g.get("batch_size", 6)),
            batch_wait=parse_duration(g.get("batch_wait"), 120.0),
            gate_timeout=gt,
            unit=specs(g.get("unit", [])),
            acceptance=specs(g.get("acceptance", [])),
            staging_start=st.get("start"),
            staging_port=int(st.get("port", 8001)),
            staging_health=st.get("health", "/"),
            staging_health_timeout=parse_duration(st.get("health_timeout"), 60.0),
            staging_stop=st.get("stop"),
            tunnel_name=tun.get("name"),
            tunnel_hostname=tun.get("hostname"),
            events_url=(raw.get("events", {}) or {}).get("url"),
            metrics_url=(raw.get("metrics", {}) or {}).get("url"),
            raw=raw,
        )


STARTER_TOML = """[project]
base_branch = "main"
ticket_prefix = "{prefix}"
work = "tasks"           # tasks: a scratch folder per ticket | code: a git branch per ticket and the merge gate
push = false

[tasks]
workers = 3              # tickets offered to workers at once (whatever fills the role takes them)
reviewers = 3
review_rounds_before_escalation = 3
aging_days_per_level = 2        # a ticket gains one priority level per N days of age; 0 disables

# [build]
# command = "uv build"
# artifact = "dist/*.whl"

# [acceptance]
# start = "{{python}} -m myapp serve --port {{port}}"   # {{python}}: a venv with {{artifact}} installed
# ready = "http://127.0.0.1:{{port}}/health"

[gate]
batch_size = 6
batch_wait = "120s"
timeout = "900s"

[[gate.unit]]
name = "unit"
command = "{unit_command}"

# [[gate.acceptance]]
# name = "acceptance"
# command = "uv run pytest tests/acceptance -q --base-url {{url}}"
#
# [[probes]]                       # a condition Orchestra checks on an interval; exit 0 = holds.
# name = "staging_fast"            # probe_failed / probe_recovered events fire on the transition,
# command = "scripts/p95_under_1s.sh $STAGING_URL"   # so agents subscribed to them wake once.
# every = "5m"
# timeout = "60s"
"""


@dataclass
class Project:
    key: str
    repo_root: Path
    config: Config

    @property
    def orchestra(self) -> OrchestraProject:
        """The same repository as Orchestra sees it (its bus, metrics store and state)."""
        return OrchestraProject(self.key, self.repo_root, OrchestraConfig.from_toml((self.repo_root / "orchestra.toml").read_text()))

    @property
    def state_dir(self) -> Path:
        return home() / self.key / "tasks"

    @property
    def tickets_file(self) -> Path:
        return self.state_dir / "tickets.json"

    @property
    def runtime_file(self) -> Path:
        return self.state_dir / "runtime.json"

    @property
    def events_file(self) -> Path:
        return home() / self.key / "events.jsonl"     # Orchestra's (the file bus), not the task system's

    @property
    def lock_file(self) -> Path:
        return self.state_dir / ".lock"

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

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

    def reload_config(self) -> None:
        self.config = Config.from_toml((self.repo_root / "orchestra.toml").read_text())


def load_project(repo: Path | str | None = None) -> Project:
    """The project for ``repo`` (default: ORCHESTRA_REPO, else the cwd), as Orchestra resolves it."""
    o = load_orchestra_project(repo)
    project = Project(key=o.key, repo_root=o.repo_root, config=Config.from_toml((o.repo_root / "orchestra.toml").read_text()))
    project.ensure_dirs()
    return project


def init_project(repo: Path, *, prefix: str | None = None, unit_command: str = "uv run pytest -q") -> Project:
    """Orchestra's starter ``orchestra.toml`` (if there is none) with the task system's sections
    added (if it has no ``[project]``), and the project registered with Orchestra."""
    from orchestra.config import init_project as init_orchestra
    root = init_orchestra(Path(repo)).repo_root
    toml_path = root / "orchestra.toml"
    text = toml_path.read_text()
    if "project" not in tomllib.loads(text):
        pfx = prefix or re.sub(r"[^A-Za-z]", "", root.name.upper())[:4] or "ORC"
        toml_path.write_text(text.rstrip() + "\n\n" + STARTER_TOML.format(prefix=pfx, unit_command=unit_command))
    return load_project(root)
