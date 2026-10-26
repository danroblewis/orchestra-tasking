"""Local JSON backend plus the runtime store. One file lock serializes every
read-modify-write across the scheduler and the web server."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..config import Project
from ..events import Events
from ..measures import Measures
from ..model import (
    ACTIVE,
    Milestone,
    MilestoneStatus,
    Note,
    State,
    Ticket,
    check_transition,
    effective_priorities,
    now,
)
from .base import Conflict, StoreError

EMPTY_TICKETS = {"tickets": {}, "milestones": {}, "notes": {}, "counters": {"ticket": 0, "milestone": 0}}
EMPTY_RUNTIME = {"worktrees": {}, "gate_runs": {}, "pipeline": {"paused": False, "reason": None}}


def _read(path: Path, empty: dict) -> dict:
    if not path.exists():
        return json.loads(json.dumps(empty))
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else json.loads(json.dumps(empty))


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class LocalStore:
    def __init__(self, project: Project, events: Events | None = None):
        self.project = project
        self.prefix = project.config.ticket_prefix
        self.events = events or Events.for_project(project)
        self.measures = Measures(project)
        project.ensure_dirs()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self.project.lock_file.open("a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _t(self) -> dict:
        return _read(self.project.tickets_file, EMPTY_TICKETS)

    def _r(self) -> dict:
        return _read(self.project.runtime_file, EMPTY_RUNTIME)

    # -- tickets ---------------------------------------------------------------

    def get_ticket(self, ticket_id: str) -> Ticket:
        d = self._t()["tickets"].get(ticket_id)
        if d is None:
            raise StoreError(f"no ticket {ticket_id}")
        return Ticket.from_dict(d)

    def list_tickets(self, *, state: State | None = None, milestone_id: str | None = None) -> list[Ticket]:
        out = [Ticket.from_dict(d) for d in self._t()["tickets"].values()]
        if state is not None:
            out = [t for t in out if t.state == state]
        if milestone_id is not None:
            out = [t for t in out if t.milestone_id == milestone_id]
        return sorted(out, key=lambda t: (t.priority, t.created_at, t.id))

    def tickets_by_id(self) -> dict[str, Ticket]:
        return {k: Ticket.from_dict(v) for k, v in self._t()["tickets"].items()}

    def create_ticket(self, **fields: Any) -> Ticket:
        with self._locked():
            data = self._t()
            ms = data["milestones"].get(fields.get("milestone_id") or "")
            if ms is None:
                raise StoreError(f"ticket needs an existing milestone; got {fields.get('milestone_id')!r}")
            if ms["status"] != MilestoneStatus.OPEN:
                raise StoreError(f"milestone {ms['id']} is closed and cannot receive tickets")
            for dep in fields.get("depends_on") or []:
                if dep not in data["tickets"]:
                    raise StoreError(f"unknown dependency {dep}")
            data["counters"]["ticket"] += 1
            tid = f"{self.prefix}-{data['counters']['ticket']}"
            t = Ticket(id=tid, **fields)
            data["tickets"][tid] = t.to_dict()
            points = self.measures.ticket_points(data["tickets"], data.setdefault("metric_counts", {}), source=self.events.source, created=True)
            _write(self.project.tickets_file, data)
        self.measures.write(points)
        self.events.emit("ticket_created", ticket=tid, title=t.title, by=t.created_by)
        return t

    def update_ticket(self, ticket_id: str, expected_version: int | None, **changes: Any) -> Ticket:
        with self._locked():
            data = self._t()
            d = data["tickets"].get(ticket_id)
            if d is None:
                raise StoreError(f"no ticket {ticket_id}")
            if expected_version is not None and d["version"] != expected_version:
                raise Conflict(f"{ticket_id} is at version {d['version']}, expected {expected_version}")
            if "milestone_id" in changes and changes["milestone_id"] != d["milestone_id"]:
                ms = data["milestones"].get(changes["milestone_id"] or "")
                if ms is None or ms["status"] != MilestoneStatus.OPEN:
                    raise StoreError("tickets can only be moved to an open milestone")
            if "depends_on" in changes:
                for dep in changes["depends_on"] or []:
                    if dep not in data["tickets"] or dep == ticket_id:
                        raise StoreError(f"bad dependency {dep}")
            if "state" in changes:
                raise StoreError("use transition() to change state")
            for k, v in changes.items():
                if k in Ticket.__dataclass_fields__:
                    d[k] = v
            d["version"] += 1
            d["updated_at"] = now()
            _write(self.project.tickets_file, data)
            t = Ticket.from_dict(d)
        self.events.emit("ticket_updated", ticket=ticket_id, fields=sorted(changes),
                         **({"owner": changes["owner"]} if "owner" in changes else {}))
        return t

    def transition(self, ticket_id: str, expected_version: int | None, to_state: State, **fields: Any) -> Ticket:
        with self._locked():
            data = self._t()
            d = data["tickets"].get(ticket_id)
            if d is None:
                raise StoreError(f"no ticket {ticket_id}")
            if expected_version is not None and d["version"] != expected_version:
                raise Conflict(f"{ticket_id} is at version {d['version']}, expected {expected_version}")
            src = State(d["state"])
            check_transition(src, to_state)
            if to_state == State.BLOCKED and not fields.get("blocked_reason"):
                raise StoreError("BLOCKED requires blocked_reason")
            for k, v in fields.items():
                if k in Ticket.__dataclass_fields__:
                    d[k] = v
            if to_state not in ACTIVE:                       # back to waiting: nobody owns it
                d["owner"] = fields.get("owner")
            d["state"] = to_state.value
            d["state_since"] = now()
            d["updated_at"] = d["state_since"]
            d["version"] += 1
            points = self.measures.ticket_points(data["tickets"], data.setdefault("metric_counts", {}), source=self.events.source,
                                                 src=src.value, dst=to_state.value)
            _write(self.project.tickets_file, data)
            t = Ticket.from_dict(d)
        self.measures.write(points)
        self.events.emit("ticket_transition", ticket=ticket_id, owner=t.owner, **{"from": src.value, "to": to_state.value})
        return t

    def add_note(self, ticket_id: str, note: Note) -> None:
        with self._locked():
            data = self._t()
            if ticket_id not in data["tickets"]:
                raise StoreError(f"no ticket {ticket_id}")
            data["notes"].setdefault(ticket_id, []).append(note.to_dict())
            _write(self.project.tickets_file, data)
        self.events.emit("note_added", ticket=ticket_id, author=note.author, kind=note.kind)

    def notes(self, ticket_id: str) -> list[dict]:
        return list(self._t()["notes"].get(ticket_id, []))

    # -- milestones ------------------------------------------------------------

    def get_milestone(self, milestone_id: str) -> Milestone:
        d = self._t()["milestones"].get(milestone_id)
        if d is None:
            raise StoreError(f"no milestone {milestone_id}")
        return Milestone.from_dict(d)

    def list_milestones(self) -> list[Milestone]:
        return sorted((Milestone.from_dict(d) for d in self._t()["milestones"].values()),
                      key=lambda m: (m.order, m.created_at))

    def milestones_by_id(self) -> dict[str, Milestone]:
        return {k: Milestone.from_dict(v) for k, v in self._t()["milestones"].items()}

    def create_milestone(self, **fields: Any) -> Milestone:
        with self._locked():
            data = self._t()
            data["counters"]["milestone"] += 1
            mid = f"M{data['counters']['milestone']}"
            fields.setdefault("order", data["counters"]["milestone"])
            m = Milestone(id=mid, **fields)
            data["milestones"][mid] = m.to_dict()
            _write(self.project.tickets_file, data)
        self.events.emit("milestone_created", milestone=mid, name=m.name)
        return m

    def update_milestone(self, milestone_id: str, expected_version: int | None, **changes: Any) -> Milestone:
        with self._locked():
            data = self._t()
            d = data["milestones"].get(milestone_id)
            if d is None:
                raise StoreError(f"no milestone {milestone_id}")
            if expected_version is not None and d["version"] != expected_version:
                raise Conflict(f"{milestone_id} is at version {d['version']}")
            if "status" in changes:
                raise StoreError("use close_milestone(); milestones cannot be reopened")
            for k in ("name", "description", "order"):
                if k in changes:
                    d[k] = changes[k]
            d["version"] += 1
            _write(self.project.tickets_file, data)
            m = Milestone.from_dict(d)
        self.events.emit("milestone_updated", milestone=milestone_id)
        return m

    def close_milestone(self, milestone_id: str, expected_version: int | None) -> Milestone:
        with self._locked():
            data = self._t()
            d = data["milestones"].get(milestone_id)
            if d is None:
                raise StoreError(f"no milestone {milestone_id}")
            if d["status"] == MilestoneStatus.CLOSED:
                raise StoreError(f"{milestone_id} is already closed")
            if expected_version is not None and d["version"] != expected_version:
                raise Conflict(f"{milestone_id} is at version {d['version']}")
            open_tickets = [t["id"] for t in data["tickets"].values()
                            if t["milestone_id"] == milestone_id and t["state"] not in ("DONE", "CANCELLED")]
            if open_tickets:
                raise StoreError(f"cannot close {milestone_id}: unfinished tickets {', '.join(open_tickets)}")
            d["status"] = MilestoneStatus.CLOSED.value
            d["closed_at"] = now()
            d["version"] += 1
            _write(self.project.tickets_file, data)
            m = Milestone.from_dict(d)
        self.events.emit("milestone_closed", milestone=milestone_id)
        return m

    # -- runtime ---------------------------------------------------------------

    def runtime(self) -> dict:
        return self._r()

    def _mutate_runtime(self, fn) -> Any:
        with self._locked():
            data = self._r()
            result = fn(data)
            _write(self.project.runtime_file, data)
            return result

    def set_worktree(self, ticket_id: str, path: str, branch: str) -> None:
        self._mutate_runtime(lambda d: d["worktrees"].__setitem__(
            ticket_id, {"ticket_id": ticket_id, "path": path, "branch": branch, "created_at": now()}))

    def remove_worktree(self, ticket_id: str) -> None:
        self._mutate_runtime(lambda d: d["worktrees"].pop(ticket_id, None))

    def worktree(self, ticket_id: str) -> dict | None:
        return self._r()["worktrees"].get(ticket_id)

    def save_gate_run(self, run: dict) -> None:
        self._mutate_runtime(lambda d: d["gate_runs"].__setitem__(run["id"], run))

    def gate_runs(self) -> list[dict]:
        return sorted(self._r()["gate_runs"].values(), key=lambda r: r.get("started_at", ""))

    # -- assignments: work the scheduler has offered a role (tasking/assignments.py) ---------

    def put_assignment(self, rec: dict) -> None:
        self._mutate_runtime(lambda d: d.setdefault("assignments", {}).__setitem__(rec["id"], rec))

    def assignments(self) -> dict[str, dict]:
        return dict(self._r().get("assignments") or {})

    def update_assignment(self, aid: str, **fields: Any) -> dict | None:
        def fn(d):
            rec = (d.get("assignments") or {}).get(aid)
            if rec is not None:
                rec.update(fields)
            return rec
        return self._mutate_runtime(fn)

    def drop_assignment(self, aid: str) -> dict | None:
        return self._mutate_runtime(lambda d: (d.get("assignments") or {}).pop(aid, None))

    def capacity(self) -> dict:
        """Effective worker/reviewer capacity. The ceiling is the operator's number when one
        is set from the Dashboard, otherwise orchestra.toml; a planner's dial (set_capacity)
        sits under that ceiling. The scheduler reads this every tick, so a change is live."""
        cfg = self.project.config
        op = self._r().get("operator_capacity") or {}
        max_w = int(op.get("workers", cfg.workers))
        max_r = int(op.get("reviewers", cfg.reviewers))
        dial = self._r().get("capacity") or {}
        return {"workers": max(0, min(int(dial.get("workers", max_w)), max_w)),
                "reviewers": max(0, min(int(dial.get("reviewers", max_r)), max_r)),
                "max_workers": max_w, "max_reviewers": max_r, "configured_workers": cfg.workers, "configured_reviewers": cfg.reviewers,
                "operator_set": bool(op), "dial_set": bool(dial), "note": dial.get("note")}

    def set_operator_capacity(self, workers: int | None, reviewers: int | None, by: str = "operator") -> dict:
        """The human's ceiling, from the Dashboard. None for both clears it back to orchestra.toml."""
        def fn(d):
            if workers is None and reviewers is None:
                d.pop("operator_capacity", None)
                return {}
            op = d.setdefault("operator_capacity", {})
            if workers is not None:
                op["workers"] = max(0, int(workers))
            if reviewers is not None:
                op["reviewers"] = max(0, int(reviewers))
            op["by"], op["updated_at"] = by, now()
            return dict(op)
        self._mutate_runtime(fn)
        cap = self.capacity()
        self.events.emit("capacity_changed", by=by, workers=cap["workers"], reviewers=cap["reviewers"], note="operator ceiling")
        return cap

    def set_capacity(self, workers: int | None, reviewers: int | None, note: str = "", by: str = "") -> dict:
        def fn(d):
            dial = d.setdefault("capacity", {})
            if workers is not None:
                dial["workers"] = int(workers)
            if reviewers is not None:
                dial["reviewers"] = int(reviewers)
            dial["note"] = note
            dial["by"] = by
            dial["updated_at"] = now()
            return dict(dial)
        out = self._mutate_runtime(fn)
        self.events.emit("capacity_changed", by=by, workers=out.get("workers"), reviewers=out.get("reviewers"), note=note)
        return self.capacity()

    def pickup_paused(self) -> bool:
        return bool(self._r().get("pickup_paused"))

    def set_pickup_paused(self, paused: bool, by: str = "") -> None:
        self._mutate_runtime(lambda d: d.__setitem__("pickup_paused", bool(paused)))
        self.events.emit("pickup_paused" if paused else "pickup_resumed", by=by)

    def request_kill(self, ticket_id: str, by: str = "") -> None:
        self._mutate_runtime(lambda d: d.setdefault("kill_requests", {}).__setitem__(ticket_id, {"by": by, "ts": now()}))
        self.events.emit("kill_requested", ticket=ticket_id, by=by)

    def probe_state(self, name: str) -> dict:
        return dict((self._r().get("probes") or {}).get(name) or {})

    def probes(self) -> list[str]:
        return list((self._r().get("probes") or {}).keys())

    def set_probe_state(self, name: str, **fields: Any) -> dict:
        def fn(d):
            st = d.setdefault("probes", {}).setdefault(name, {"name": name})
            st.update(fields)
            return dict(st)
        return self._mutate_runtime(fn)

    def take_kill_requests(self) -> list[str]:
        def fn(d):
            ids = list(d.get("kill_requests", {}).keys())
            d["kill_requests"] = {}
            return ids
        return self._mutate_runtime(fn)

    def pipeline(self) -> dict:
        return self._r()["pipeline"]

    def set_pipeline(self, paused: bool, reason: str | None = None) -> None:
        self._mutate_runtime(lambda d: d.__setitem__("pipeline", {"paused": paused, "reason": reason}))
        self.events.emit("pipeline_paused" if paused else "pipeline_resumed", reason=reason)

    # -- snapshots -------------------------------------------------------------

    def snapshot(self) -> dict:
        t = self._t()
        r = self._r()
        tickets = [Ticket.from_dict(d) for d in t["tickets"].values()]
        counts = {s.value: 0 for s in State}
        for x in tickets:
            counts[x.state.value] += 1
        by_id = {x.id: x for x in tickets}
        milestones = {k: Milestone.from_dict(v) for k, v in t["milestones"].items()}
        running_workers = sum(1 for a in (r.get("assignments") or {}).values() if a.get("role") == "worker")
        cap = self.capacity()
        eff = effective_priorities(by_id, self.project.config.aging_days_per_level)

        def ticket_view(x: Ticket) -> dict:
            d = x.to_dict()
            d["priority_info"] = eff.get(x.id)
            d["effective_priority"] = eff.get(x.id, {}).get("effective", x.priority)
            if x.state == State.OPEN:
                waiting = [dep for dep in x.depends_on if dep not in by_id or by_id[dep].state != State.DONE]
                ms = milestones.get(x.milestone_id)
                d["waiting_on"] = waiting
                d["ready"] = not waiting and ms is not None and ms.status == MilestoneStatus.OPEN
                d["why_waiting"] = (f"waiting on {', '.join(waiting)}" if waiting
                                    else "milestone is closed" if ms is None or ms.status != MilestoneStatus.OPEN
                                    else "ready; worker capacity is 0" if cap["workers"] == 0
                                    else f"ready; waiting for a worker slot ({running_workers}/{cap['workers']} in use)")
            return d
        return {
            "project": {"key": self.project.key, "repo_root": str(self.project.repo_root),
                        "base_branch": self.project.config.base_branch, "work": self.project.config.work},
            "tickets": [ticket_view(x) for x in sorted(tickets, key=lambda x: (eff.get(x.id, {}).get("effective", x.priority), x.created_at))],
            "workers": cap["workers"],
            "capacity": cap,
            "milestones": [d for d in sorted(t["milestones"].values(), key=lambda m: m["order"])],
            "assignments": list((r.get("assignments") or {}).values()),
            "worktrees": list(r["worktrees"].values()),
            "gate_runs": sorted(r["gate_runs"].values(), key=lambda g: g.get("started_at", ""))[-20:],
            "pipeline": r["pipeline"],
            "pickup_paused": bool(r.get("pickup_paused")),
            "probes": [p for p in (r.get("probes") or {}).values() if not p.get("removed")],
            "counts": counts,
            "active": sum(1 for x in tickets if x.state in ACTIVE),
        }
