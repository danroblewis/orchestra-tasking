"""The runtime half of the store: what the scheduler holds while it runs, in ``runtime.json``:
assignments, worktrees, gate runs, capacity, pickup, kill requests, probes, the pipeline, the
scheduler's own record, staging and builds. The operator's changes are observations on the bus
(``capacity.changed``, ``pickup.*``, ``kill.requested``, ``pipeline.*``)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..model import now
from .files import read, write

EMPTY_RUNTIME = {"assignments": {}, "worktrees": {}, "gate_runs": {}, "builds": {}, "probes": {}, "kill_requests": {},
                 "capacity": {}, "operator_capacity": {}, "pickup_paused": False, "scheduler": None, "staging": {},
                 "pipeline": {"paused": False, "reason": None}}


class Runtime:
    """Half of ``LocalStore`` (store/local.py), which supplies ``project``, ``events`` and ``_locked``."""

    def _r(self) -> dict:
        data = read(self.project.runtime_file, EMPTY_RUNTIME)
        for k, v in EMPTY_RUNTIME.items():                  # a file from before a key existed
            data.setdefault(k, v)
        return data

    def _mutate_runtime(self, fn: Callable[[dict], Any]) -> Any:
        with self._locked():
            data = self._r()
            result = fn(data)
            write(self.project.runtime_file, data)
            return result

    def put_assignment(self, rec: dict) -> None:
        self._mutate_runtime(lambda d: d["assignments"].__setitem__(rec["id"], rec))

    def assignments(self) -> dict[str, dict]:
        return dict(self._r()["assignments"])

    def update_assignment(self, aid: str, **fields: Any) -> dict | None:
        def fn(d):
            rec = d["assignments"].get(aid)
            if rec is not None:
                rec.update(fields)
            return rec
        return self._mutate_runtime(fn)

    def drop_assignment(self, aid: str) -> dict | None:
        return self._mutate_runtime(lambda d: d["assignments"].pop(aid, None))

    def set_worktree(self, ticket_id: str, path: str, branch: str) -> None:
        self._mutate_runtime(lambda d: d["worktrees"].__setitem__(
            ticket_id, {"ticket_id": ticket_id, "path": path, "branch": branch, "created_at": now()}))

    def remove_worktree(self, ticket_id: str) -> None:
        self._mutate_runtime(lambda d: d["worktrees"].pop(ticket_id, None))

    def worktree(self, ticket_id: str) -> dict | None:
        return self._r()["worktrees"].get(ticket_id)

    def worktrees(self) -> dict[str, dict]:
        return dict(self._r()["worktrees"])

    def save_gate_run(self, run: dict) -> None:
        self._mutate_runtime(lambda d: d["gate_runs"].__setitem__(run["id"], run))

    def gate_runs(self) -> list[dict]:
        return sorted(self._r()["gate_runs"].values(), key=lambda r: r.get("started_at", ""))

    def record_build(self, build: dict) -> None:
        self._mutate_runtime(lambda d: d["builds"].__setitem__(build["id"], build))

    def builds(self) -> dict[str, dict]:
        return dict(self._r()["builds"])

    def capacity(self) -> dict:
        """Effective worker/reviewer capacity. The ceiling is the operator's number when one
        is set from the Dashboard, otherwise orchestra.toml; a planner's dial (set_capacity)
        sits under that ceiling. The scheduler reads this every tick, so a change is live."""
        cfg = self.project.config
        r = self._r()
        op, dial = r["operator_capacity"], r["capacity"]
        max_w = int(op.get("workers", cfg.workers))
        max_r = int(op.get("reviewers", cfg.reviewers))
        return {"workers": max(0, min(int(dial.get("workers", max_w)), max_w)),
                "reviewers": max(0, min(int(dial.get("reviewers", max_r)), max_r)),
                "max_workers": max_w, "max_reviewers": max_r, "note": dial.get("note")}

    def set_operator_capacity(self, workers: int | None, reviewers: int | None, by: str = "operator") -> dict:
        """The human's ceiling, from the Dashboard. None for both clears it back to orchestra.toml."""
        def fn(d):
            if workers is None and reviewers is None:
                d["operator_capacity"] = {}
                return
            op = d["operator_capacity"]
            if workers is not None:
                op["workers"] = max(0, int(workers))
            if reviewers is not None:
                op["reviewers"] = max(0, int(reviewers))
            op["by"], op["updated_at"] = by, now()
        self._mutate_runtime(fn)
        cap = self.capacity()
        self.events.emit("capacity.changed", by=by, workers=cap["workers"], reviewers=cap["reviewers"], note="operator ceiling")
        return cap

    def set_capacity(self, workers: int | None, reviewers: int | None, note: str = "", by: str = "") -> dict:
        def fn(d):
            dial = d["capacity"]
            if workers is not None:
                dial["workers"] = int(workers)
            if reviewers is not None:
                dial["reviewers"] = int(reviewers)
            dial.update(note=note, by=by, updated_at=now())
            return dict(dial)
        out = self._mutate_runtime(fn)
        self.events.emit("capacity.changed", by=by, workers=out.get("workers"), reviewers=out.get("reviewers"), note=note)
        return self.capacity()

    def pickup_paused(self) -> bool:
        return bool(self._r()["pickup_paused"])

    def set_pickup_paused(self, paused: bool, by: str = "") -> None:
        self._mutate_runtime(lambda d: d.__setitem__("pickup_paused", bool(paused)))
        self.events.emit("pickup.paused" if paused else "pickup.resumed", by=by)

    def request_kill(self, ticket_id: str, by: str = "") -> None:
        self._mutate_runtime(lambda d: d["kill_requests"].__setitem__(ticket_id, {"by": by, "ts": now()}))
        self.events.emit("kill.requested", ticket=ticket_id, by=by)

    def take_kill_requests(self) -> list[str]:
        def fn(d):
            ids = list(d["kill_requests"])
            d["kill_requests"] = {}
            return ids
        return self._mutate_runtime(fn)

    def probe_state(self, name: str) -> dict:
        return dict(self._r()["probes"].get(name) or {})

    def probe_states(self) -> dict[str, dict]:
        return dict(self._r()["probes"])

    def set_probe_state(self, name: str, **fields: Any) -> dict:
        def fn(d):
            st = d["probes"].setdefault(name, {"name": name})
            st.update(fields)
            return dict(st)
        return self._mutate_runtime(fn)

    def pipeline(self) -> dict:
        return self._r()["pipeline"]

    def set_pipeline(self, paused: bool, reason: str | None = None) -> None:
        self._mutate_runtime(lambda d: d.__setitem__("pipeline", {"paused": paused, "reason": reason}))
        self.events.emit("pipeline.paused" if paused else "pipeline.resumed", reason=reason)

    def scheduler(self) -> dict | None:
        """The running scheduler's record (pid, started_at), or None."""
        return self._r()["scheduler"]

    def set_scheduler(self, pid: int, started_at: float) -> None:
        self._mutate_runtime(lambda d: d.__setitem__("scheduler", {"pid": pid, "started_at": started_at}))

    def clear_scheduler(self, pid: int) -> None:
        """Only the scheduler that wrote the record clears it: a replacement may already have started."""
        self._mutate_runtime(lambda d: d.__setitem__("scheduler", None) if (d["scheduler"] or {}).get("pid") == pid else None)

    def staging(self) -> dict:
        return dict(self._r()["staging"])

    def set_staging(self, **fields: Any) -> dict:
        def fn(d):
            d["staging"].update(fields)
            return dict(d["staging"])
        return self._mutate_runtime(fn)
