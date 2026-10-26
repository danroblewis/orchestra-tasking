"""The task system in Orchestra's web app (an Orchestra plugin, ``orchestra/plugins.py``): the
Tickets page (the ticket graph), the Tasks panel, the scheduler's machine card, and the API they
read, at ``/api/plugins/tasks``. The operator controls (pause pickup, capacity, stop whoever works
a ticket) are the task system's own write paths. The agents are orchestra-agents' plugin."""

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
        project.reload_config()
        snap = store.snapshot()
        snap["now"] = time.time()
        snap["gate_runs"] = store.gate_runs()[-30:]
        snap["batch_size"] = 1  # the gate takes one ticket per run until batching lands
        snap["loop"] = scheduler()
        return snap

    @r.get("/landings")
    async def landings(hours: int = 24, bucket: int = 1) -> dict:
        """Tickets landed on the base branch per bucket (bucket = hours per bar), from gate runs."""
        from datetime import datetime, timezone
        hours = max(1, min(hours, 24 * 30))
        bucket = max(1, min(bucket, 24))
        bsec = bucket * 3600
        now_ts = time.time()
        n = max(1, hours // bucket)
        start = int(now_ts // bsec) * bsec - (n - 1) * bsec
        buckets = [{"t": start + i * bsec, "landed": 0, "runs": 0, "failed": 0} for i in range(n)]
        for r in store.gate_runs():
            if not r.get("finished_at"):
                continue
            try:
                ts = datetime.fromisoformat(r["finished_at"]).replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                continue
            i = int((ts - start) // bsec)
            if 0 <= i < n:
                buckets[i]["runs"] += 1
                if r.get("status") == "landed":
                    buckets[i]["landed"] += len(r.get("merged") or [])
                elif r.get("status") in ("rejected", "base_broken", "error", "conflict"):
                    buckets[i]["failed"] += 1
        total = sum(b["landed"] for b in buckets)
        return {"hours": hours, "bucket": bucket, "buckets": buckets, "total": total, "per_hour": round(total / hours, 2)}

    @r.get("/burndown")
    async def burndown(hours: int = 24) -> dict:
        """Remaining (not DONE/CANCELLED) and total tickets over time, from the event log."""
        from datetime import datetime, timezone
        hours = max(1, min(hours, 24 * 30))
        since = time.time() - hours * 3600
        evs, _ = events.read(0, limit=200000)
        total = remaining = 0
        series: list[dict] = []
        for e in evs:
            t = e.get("type")
            if t not in ("ticket_created", "ticket_transition"):
                continue
            try:
                ts = datetime.fromisoformat(e["ts"]).replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                continue
            if t == "ticket_created":
                total += 1; remaining += 1
            else:
                to, frm = e.get("to"), e.get("from")
                if to in ("DONE", "CANCELLED") and frm not in ("DONE", "CANCELLED"):
                    remaining -= 1
            if ts >= since:
                if not series:  # anchor the left edge at the window start with the value then
                    series.append({"t": since, "remaining": remaining, "total": total})
                series.append({"t": ts, "remaining": remaining, "total": total})
        # Carry the current value to now so the chart reaches the right edge.
        if not series:
            series.append({"t": since, "remaining": remaining, "total": total})
        series.append({"t": time.time(), "remaining": remaining, "total": total})
        return {"hours": hours, "series": series[-2000:], "remaining": remaining, "total": total}

    @r.get("/staging")
    async def staging() -> dict:
        from .staging import Staging
        project.reload_config()
        st = Staging(project, store, events)
        state = st.state()
        build = store.runtime().get("builds", {}).get(state.get("current_build_id") or "")
        return {**state, "build": build, "log": st.logs(), "health_path": project.config.staging_health}

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
        """What waits on a human: blocked tickets (a worker could not go on). Read on every page
        (static/attention.js)."""
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
                           "assignment": ({"role": a["role"], "instance": a.get("instance"), "since": a.get("assigned_at"),
                                           "session": a.get("session")} if a else None)})
        flight.sort(key=lambda x: (x["assignment"] is None, x["state_since"] or ""))
        counts: dict[str, int] = {}
        for t in snap["tickets"]:
            counts[t["state"]] = counts.get(t["state"], 0) + 1
        return {"ok": True, "now": time.time(), "tickets": flight, "counts": counts, "capacity": store.capacity(),
                "pickup_paused": store.pickup_paused(), "scheduler": scheduler()["alive"]}

    return r
