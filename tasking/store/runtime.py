"""The runtime half of the store: what the scheduler holds while it runs, in ``runtime.json``:
assignments, the offers nobody took, worktrees, the worktree pool, gate runs, capacity, pickup, kill requests, probes, the pipeline, the
scheduler's own record, staging and builds. The operator's changes are published as the operator's
console (``task_web``: ``capacity.changed``, ``pickup.*``, ``kill.requested``) and the gate's pause
as the gate (``pipeline.paused``); a change a planner asked for is answered by ``request.done``
alone, since its request already said what it asks (tasking/events.py)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..events import GATE, OPERATOR
from ..model import now
from .files import read, write

EMPTY_RUNTIME = {"assignments": {}, "untaken": {}, "worktrees": {}, "pool": {}, "gate_runs": {}, "builds": {}, "probes": {}, "kill_requests": {},
                 "capacity": {}, "operator_capacity": {}, "pickup_paused": False, "scheduler": None, "staging": {},
                 "pipeline": {"paused": False, "reason": None}, "desk": None, "pool_waiting": 0}


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

    def hand_over(self, aid: str, rec: dict) -> None:
        """One turn ends and the next is offered in its place (a review requested, changes
        requested): the assignment ``aid`` is dropped and ``rec`` recorded, and a pool worktree
        ``aid`` held passes to ``rec`` with the work, in one write."""
        def fn(d):
            d["assignments"].pop(aid, None)
            d["assignments"][rec["id"]] = rec
            for s in d["pool"].values():
                if s.get("holder") == aid:
                    s["holder"] = rec["id"]
        self._mutate_runtime(fn)

    def untaken(self) -> dict[str, dict]:
        """The offers nobody took within their lease, by ``<ticket>/<role>``: ticket, role, since
        (the first of them), misses (how many), version (the ticket's, when last offered) and
        again_at (when the next offer may go out)."""
        return dict(self._r()["untaken"])

    def set_untaken(self, ticket_id: str, role: str, rec: dict | None) -> None:
        """Record an untaken offer, or (``rec`` None) forget it: it was taken, or the ticket moved on."""
        key = f"{ticket_id}/{role}"
        self._mutate_runtime(lambda d: d["untaken"].__setitem__(key, rec) if rec else d["untaken"].pop(key, None))

    def offer_untaken_now(self) -> None:
        """Every untaken offer may go out again at once (a machine that may take it has started)."""
        self._mutate_runtime(lambda d: [r.__setitem__("again_at", 0.0) for r in d["untaken"].values()])

    def forget_untaken(self) -> None:
        self._mutate_runtime(lambda d: d.__setitem__("untaken", {}))

    def set_worktree(self, ticket_id: str, path: str, branch: str) -> None:
        self._mutate_runtime(lambda d: d["worktrees"].__setitem__(
            ticket_id, {"ticket_id": ticket_id, "path": path, "branch": branch, "created_at": now()}))

    def remove_worktree(self, ticket_id: str) -> None:
        self._mutate_runtime(lambda d: d["worktrees"].pop(ticket_id, None))

    def worktree(self, ticket_id: str) -> dict | None:
        return self._r()["worktrees"].get(ticket_id)

    def worktrees(self) -> dict[str, dict]:
        return dict(self._r()["worktrees"])

    def pool(self) -> dict[str, dict]:
        """The worktree pool's slots by name (tasking/pool.py): path, holder (an assignment id, or
        ``gate:<ticket>`` for a gate run, or None), ticket (the last one it was reset for) and
        since (when it was last claimed), each with ``free`` worked out as ``claim_slot`` does."""
        r = self._r()
        return {n: {**s, "free": _free(s, r["assignments"])} for n, s in r["pool"].items()}

    def claim_slot(self, holder: str, ticket: str, size: int, prefer: str | None = None) -> tuple[dict, bool] | None:
        """A free slot for ``holder``, or a new one while there are fewer than ``size``; None when
        the pool is full. A slot is free when nobody holds it, or its holder is an assignment that
        is no longer recorded (released, settled, or never offered): the assignments are the
        truth, so whatever drops one frees its worktree. A gate's hold lasts until it is
        released (``release_slot``) or the scheduler restarts. The slot that last served
        ``prefer`` comes first, then the one claimed longest ago. Returns the slot and whether it
        is new (its worktree is still to be made)."""
        def fn(d):
            slots = d["pool"]
            free = [s for s in slots.values() if _free(s, d["assignments"])]
            pick = next((s for s in free if prefer and s.get("ticket") == prefer), None) \
                or min(free, key=lambda s: s.get("since") or 0, default=None)
            new = pick is None
            if new:
                if len(slots) >= size:
                    return None
                name = next(str(i) for i in range(1, len(slots) + 2) if str(i) not in slots)
                pick = slots[name] = {"name": name, "path": str(self.project.pool_dir / name)}
            pick.update(holder=holder, ticket=ticket, since=now())
            return dict(pick), new
        return self._mutate_runtime(fn)

    def release_slot(self, holder: str) -> None:
        self._mutate_runtime(lambda d: [s.__setitem__("holder", None) for s in d["pool"].values() if s.get("holder") == holder])

    def release_gate_slots(self) -> None:
        """Every gate hold dropped (a restart: no gate runs any more)."""
        self._mutate_runtime(lambda d: [s.__setitem__("holder", None) for s in d["pool"].values()
                                        if str(s.get("holder") or "").startswith("gate:")])

    def forget_slot(self, name: str) -> None:
        self._mutate_runtime(lambda d: d["pool"].pop(name, None))

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
        self.events.emit("capacity.changed", src=OPERATOR, by=by, workers=cap["workers"], reviewers=cap["reviewers"], note="operator ceiling")
        return cap

    def set_capacity(self, workers: int | None, reviewers: int | None, note: str = "", by: str = "") -> dict:
        """The planner's dial, on its request: answered by ``request.done``, not published again."""
        def fn(d):
            dial = d["capacity"]
            if workers is not None:
                dial["workers"] = int(workers)
            if reviewers is not None:
                dial["reviewers"] = int(reviewers)
            dial.update(note=note, by=by, updated_at=now())
            return dict(dial)
        self._mutate_runtime(fn)
        return self.capacity()

    def pickup_paused(self) -> bool:
        return bool(self._r()["pickup_paused"])

    def set_pickup_paused(self, paused: bool, by: str = "") -> None:
        self._mutate_runtime(lambda d: d.__setitem__("pickup_paused", bool(paused)))
        self.events.emit("pickup.paused" if paused else "pickup.resumed", src=OPERATOR, by=by)

    def request_kill(self, ticket_id: str, by: str = "") -> None:
        self._mutate_runtime(lambda d: d["kill_requests"].__setitem__(ticket_id, {"by": by, "ts": now()}))
        self.events.emit("kill.requested", src=OPERATOR, ticket=ticket_id, by=by)

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

    def set_pipeline(self, paused: bool, reason: str | None = None, announce: bool = True) -> None:
        """``announce`` False: a planner's request to resume, answered by ``request.done``."""
        self._mutate_runtime(lambda d: d.__setitem__("pipeline", {"paused": paused, "reason": reason}))
        if announce:
            self.events.emit("pipeline.paused" if paused else "pipeline.resumed", src=GATE, reason=reason)   # the pipeline is the gate's

    def set_pool_waiting(self, n: int) -> None:
        """How many turns wait for a worktree of the pool (``tasks_worktrees{state="waiting"}``)."""
        if int(self._r().get("pool_waiting") or 0) != n:
            self._mutate_runtime(lambda d: d.__setitem__("pool_waiting", int(n)))

    def scheduler(self) -> dict | None:
        """The running scheduler's record (pid, started_at), or None."""
        return self._r()["scheduler"]

    def set_scheduler(self, pid: int, started_at: float) -> None:
        self._mutate_runtime(lambda d: d.__setitem__("scheduler", {"pid": pid, "started_at": started_at}))

    def clear_scheduler(self, pid: int) -> None:
        """Only the scheduler that wrote the record clears it: a replacement may already have started."""
        self._mutate_runtime(lambda d: d.__setitem__("scheduler", None) if (d["scheduler"] or {}).get("pid") == pid else None)

    def desk(self) -> dict | None:
        """Where the desk reads requests from next (a bus offset) and the request ids it answered
        from there on, or None before a desk ever ran. A desk that starts reads on from here."""
        return self._r()["desk"]

    def set_desk(self, offset: int, answered: list[str]) -> None:
        self._mutate_runtime(lambda d: d.__setitem__("desk", {"offset": offset, "answered": list(answered), "at": now()}))

    def staging(self) -> dict:
        return dict(self._r()["staging"])

    def set_staging(self, **fields: Any) -> dict:
        def fn(d):
            d["staging"].update(fields)
            return dict(d["staging"])
        return self._mutate_runtime(fn)


def _free(slot: dict, assignments: dict) -> bool:
    holder = slot.get("holder")
    return holder is None or (not holder.startswith("gate:") and holder not in assignments)
