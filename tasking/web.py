"""The task system in Orchestra's web app (an Orchestra plugin, ``orchestra/plugins.py``): the
Tickets page (the ticket graph), the Tasks panel, the scheduler's machine card, the blocked-ticket
banner, and the API they read, at ``/api/plugins/tasks``. The operator controls (pause pickup,
capacity, stop whoever works a ticket) are the only writes here; tickets change only through the
scheduler (tasking/requests.py)."""

from __future__ import annotations

import time
from pathlib import Path

import psutil
from fastapi import APIRouter, HTTPException, Request
from orchestra.plugins import Declared, Page, Plugin

from .config import load_project
from .events import Events
from .model import State
from .store import LocalStore, StoreError

STATIC = Path(__file__).parent / "static"


def _alive(pid) -> bool:
    """A recorded pid proves liveness only if that process exists and is not a zombie."""
    try:
        return bool(pid) and psutil.Process(int(pid)).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


def plugin(orchestra_project) -> Plugin:
    """The plugin for the repository Orchestra serves (its entry point, in pyproject.toml)."""
    project = load_project(orchestra_project.repo_root)
    return Plugin("tasks", [Page("/tickets", "Tickets", STATIC / "tickets.html")],
                  STATIC, router(project), scripts=["attention.js", "machine-cards.js", "tasks-panel.js"], machines=declared(project))


def declared(project) -> list[Declared]:
    """The machine the task system provides, for Orchestra's Machines page before it runs."""
    return [Declared("task_scheduler", "program", "Offers ready tickets to workers and reviewers and applies their reports.", "continuous")]


def router(project) -> APIRouter:
    r = APIRouter()
    events = Events.for_project(project, source="tasks-web")
    store = LocalStore(project, events)

    def scheduler() -> dict:
        s = store.runtime().get("scheduler") or {}
        return {**s, "alive": _alive(s.get("pid"))}

    @r.get("/state")
    async def state() -> dict:
        """The Tickets page: the snapshot (tickets with their readiness, milestones, assignments,
        capacity, the pipeline), the last gate runs and whether the scheduler runs."""
        project.reload_config()
        snap = store.snapshot()
        snap["now"] = time.time()
        snap["gate_runs"] = store.gate_runs()[-30:]
        snap["loop"] = scheduler()
        return snap

    @r.post("/control")
    async def control(request: Request) -> dict:
        """Operator controls (not ticket editing): pause/resume new worker pickup, capacity, stop whoever works a ticket."""
        body = await request.json()
        action = body.get("action")
        if action in ("pause", "resume"):
            store.set_pickup_paused(action == "pause", by="operator")
            return {"ok": True, "pickup_paused": action == "pause"}
        if action == "capacity":
            # The operator's ceiling on workers and reviewers, live. Running turns finish;
            # only new pickups see the new number. Omit both to return to orchestra.toml.
            try:
                w = int(body["workers"]) if body.get("workers") is not None else None
                r = int(body["reviewers"]) if body.get("reviewers") is not None else None
            except (TypeError, ValueError):
                raise HTTPException(400, "workers and reviewers must be whole numbers")
            if (w is not None and not 0 <= w <= 64) or (r is not None and not 0 <= r <= 64):
                raise HTTPException(400, "capacity must be between 0 and 64")
            return {"ok": True, "capacity": store.set_operator_capacity(w, r, by="operator")}
        if action == "kill":
            tid = body.get("ticket") or ""
            try:
                t = store.get_ticket(tid)
            except StoreError:
                raise HTTPException(404)
            if t.state not in (State.IN_PROGRESS, State.IN_REVIEW):
                raise HTTPException(409, f"{tid} is {t.state}; nothing to stop")
            store.request_kill(tid, by="operator")
            return {"ok": True, "ticket": tid, "will_reset_to": "OPEN" if t.state == State.IN_PROGRESS else "READY_FOR_REVIEW"}
        raise HTTPException(400, "unknown action")

    @r.get("/tickets/{ticket_id}")
    async def ticket(ticket_id: str) -> dict:
        try:
            t = store.get_ticket(ticket_id)
        except StoreError:
            raise HTTPException(404)
        return {**t.to_dict(), "notes": store.notes(ticket_id), "worktree": store.worktree(ticket_id)}

    @r.get("/gate/runs/{run_id}")
    async def gate_run(run_id: str) -> dict:
        for r in store.gate_runs():
            if r["id"] == run_id:
                return r
        raise HTTPException(404)

    @r.get("/attention")
    async def attention() -> dict:
        """What waits on a planner's decision: blocked tickets (a worker could not go on). Read on
        every page (static/attention.js)."""
        snap = store.snapshot()
        blocked = [{"id": t["id"], "title": t["title"], "reason": t.get("blocked_reason") or "", "since": t.get("state_since"),
                    "milestone": t.get("milestone_id")} for t in snap["tickets"] if t["state"] == "BLOCKED"]
        return {"ok": True, "blocked": blocked, "count": len(blocked)}

    @r.get("/board")
    async def board() -> dict:
        """The Tasks panel: every ticket in flight (not waiting to start, not finished), with the
        machine instance it is assigned to now, newest activity first; and the queue by state, the
        capacity and the pause."""
        snap = store.snapshot()
        held = {a["ticket"]: a for a in store.assignments().values()}
        waiting = {"READY_FOR_REVIEW": "waiting for a reviewer", "REJECTED": "rejected: waiting for its worker",
                   "APPROVED": "approved", "BLOCKED": "blocked: waiting for a decision"}
        flight = []
        for t in snap["tickets"]:
            if t["state"] in ("OPEN", "DONE", "CANCELLED"):
                continue
            a = held.get(t["id"])
            flight.append({**{k: t.get(k) for k in ("id", "title", "state", "owner", "state_since", "worker_turns", "reviewer_rejections",
                                                       "gate_rejections", "blocked_reason", "milestone_id")},
                           "waiting": None if a else waiting.get(t["state"], ""),
                           "assignment": ({"role": a["role"], "instance": a.get("instance"), "since": a.get("assigned_at")} if a else None)})
        flight.sort(key=lambda x: (x["assignment"] is None, x["state_since"] or ""))
        counts: dict[str, int] = {}
        for t in snap["tickets"]:
            counts[t["state"]] = counts.get(t["state"], 0) + 1
        return {"ok": True, "now": time.time(), "tickets": flight, "counts": counts, "capacity": store.capacity(),
                "pickup_paused": store.pickup_paused(), "scheduler": scheduler()["alive"]}

    return r
