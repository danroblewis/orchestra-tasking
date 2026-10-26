"""Probes: conditions Orchestra checks on an interval and turns into events.

A probe is a command with a schedule, declared in ``orchestra.toml`` (``[[probes]]``). Exit 0
means the condition holds. Orchestra remembers each probe's last state and emits
``probe_failed`` and ``probe_recovered`` only on the transition, so a failing probe wakes
the agents subscribed to it once, not every interval. Agents never run probes; Orchestra
supplies the clock and the event, the repository or the team supplies the check."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Project
from .events import Events
from .store import LocalStore

OUTPUT_TAIL = 2000


def parse_interval(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+)\s*(s|m|h|d)\s*", text)
    if not m:
        raise ValueError("every must look like 30s, 30m, 6h, or 1d")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


@dataclass(frozen=True)
class ProbeSpec:
    name: str
    command: str
    every: float                 # seconds
    timeout: float = 120.0
    cwd: Path | None = None
    team: str | None = None
    path_dirs: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


def _spec(d: dict, project: Project) -> ProbeSpec:
    every = d.get("every", "5m")
    secs = parse_interval(every) if isinstance(every, str) else float(every)
    return ProbeSpec(name=d["name"], command=d["command"], every=secs, timeout=float(d.get("timeout", 120)), cwd=project.repo_root)


def config_probes(project: Project) -> list[ProbeSpec]:
    out = []
    for d in project.config.raw.get("probes", []) or []:
        try:
            out.append(_spec(d, project))
        except (KeyError, ValueError):
            continue
    return out


def staging_url(project: Project) -> str:
    cfg = project.config
    return f"https://{cfg.tunnel_hostname}" if cfg.tunnel_hostname else f"http://127.0.0.1:{cfg.staging_port}"


class Prober:
    """Runs due probes in the background and records their state. One thread per probe
    at a time; a probe that is still running when it is next due is simply skipped."""

    def __init__(self, project: Project, store: LocalStore, events: Events):
        self.project, self.store, self.events = project, store, events
        self.running: dict[str, threading.Thread] = {}

    def tick(self, specs: list[ProbeSpec]) -> None:
        now_t = time.time()
        for k in [k for k, th in self.running.items() if not th.is_alive()]:
            self.running.pop(k)
        known = {s.name for s in specs}
        for s in specs:
            st = self.store.probe_state(s.name)
            if s.name in self.running or (st.get("last_run") and now_t - st["last_run"] < s.every):
                continue
            th = threading.Thread(target=self._run, args=(s,), daemon=True, name=f"probe-{s.name}")
            self.running[s.name] = th
            th.start()
        for name in self.store.probes():
            if name not in known and not self.store.probe_state(name).get("removed"):
                self.store.set_probe_state(name, removed=True)

    def _run(self, s: ProbeSpec) -> None:
        env = {**os.environ, **s.env, "ORCHESTRA_REPO": str(self.project.repo_root), "ORCHESTRA_PROBE": s.name,
               "STAGING_URL": staging_url(self.project)}
        if s.path_dirs:
            env["PATH"] = os.pathsep.join([*s.path_dirs, env.get("PATH", "")])
        started = time.time()
        try:
            p = subprocess.run(s.command, shell=True, cwd=str(s.cwd or self.project.repo_root), env=env,
                               capture_output=True, text=True, timeout=s.timeout, check=False)
            ok, output = p.returncode == 0, (p.stdout + p.stderr)[-OUTPUT_TAIL:]
        except subprocess.TimeoutExpired as e:
            ok, output = False, f"timed out after {s.timeout:.0f}s\n" + ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))[-OUTPUT_TAIL:]
        except OSError as e:
            ok, output = False, f"could not run: {e}"
        status = "ok" if ok else "failed"
        prev = self.store.probe_state(s.name).get("status")
        fields = {"status": status, "last_run": started, "duration": time.time() - started, "output": output,
                  "every": s.every, "team": s.team, "command": s.command, "removed": False}
        if prev != status:
            fields["since"] = started
        self.store.set_probe_state(s.name, **fields)
        if status == "failed" and prev != "failed":
            self.events.emit("probe_failed", probe=s.name, team=s.team, output=output[-600:])
        elif status == "ok" and prev == "failed":
            self.events.emit("probe_recovered", probe=s.name, team=s.team)
