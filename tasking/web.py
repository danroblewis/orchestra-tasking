"""The task system in Orchestra's web app (an Orchestra plugin, ``orchestra/plugins.py``): the
Tickets page (the ticket graph), the Tasks panel, the blocked-ticket
banner, and the API they read, at ``/api/plugins/tasks``; the scheduler, which ``orchestra run``
keeps running (``Plugin.processes``) and which is no machine (tasking/events.py), so nothing is
declared for it; and the instrument that reads the task system's measurements
(``Plugin.collectors``: ``tasks.tickets``, tasking/measures.py), also served at ``/metrics``. The operator controls (pause pickup,
capacity, stop whoever works a ticket) are the only writes here, published as the machine
``task_web``; tickets change only through the scheduler (tasking/requests.py)."""

from __future__ import annotations

import shlex
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from orchestra.collect.spec import Collector
from orchestra.launcher import Process
from orchestra.plugins import Page, Plugin
from orchestra.runtime import alive

from .config import Project
from .events import OPERATOR, Events
from .store import LocalStore, StoreError

STATIC = Path(__file__).parent / "static"
WEB = OPERATOR


def plugin(orchestra_project) -> Plugin:
    """The plugin for the repository Orchestra serves (its entry point, in pyproject.toml)."""
    project = Project.from_orchestra(orchestra_project)
    return Plugin("tasks", [Page("/tickets", "Tickets", STATIC / "tickets.html")],
                  STATIC, router(project), scripts=["tasks-boot.js"], stylesheets=["tasks.css"],
                  processes=processes(project), collectors=collectors(project))


def set_up(project: Project) -> bool:
    """Whether the project is set up for tasks (``tasking init`` wrote ``[project]`` or ``[tasks]``)."""
    return bool({"project", "tasks"} & set(project.config.raw))


def processes(project: Project) -> list[Process]:
    """What ``orchestra run`` keeps running for the task system: the scheduler, in a project set up
    for tasks (``tasking init`` wrote ``[project]`` or ``[tasks]`` in its orchestra.toml). It is
    started with the Python that runs Orchestra, where the task system is installed; ``--wait``
    lets one started by hand keep the project until it stops. Orchestra names it ``tasks.scheduler``
    (its log: ``<state>/logs/machines/tasks.scheduler.log``)."""
    if not set_up(project):
        return []
    return [Process("scheduler", [sys.executable, "-m", "tasking", "run", "--wait"])]


def collectors(project: Project) -> list[Collector]:
    """The instrument that measures the tickets (``tasks.tickets``): ``orchestra run`` runs
    ``tasking metrics`` every discovery step, which prints the levels and counters from the record
    (tasking/measures.py), and labels each point ``instrument=tasks.tickets``. A command, not a
    scrape of ``/metrics``: the web server's port is not known to ``orchestra run``, and a port of
    the scheduler's own would be one more to configure per project; the text is the same."""
    if not set_up(project):
        return []
    step = max(1.0, float(project.orchestra.config.discovery_step))
    return [Collector.collect("tickets", f"{shlex.quote(sys.executable)} -m tasking metrics", every_s=step, timeout_s=min(step, 10.0))]


def router(project: Project) -> APIRouter:
    def fresh() -> None:
        project.reload_config()                       # the configuration, read again through Orchestra per request: the capacity ceiling is live

    r = APIRouter(dependencies=[Depends(fresh)])
    store = LocalStore(project, Events.for_project(project, source=WEB))

    def scheduler() -> dict:
        s = store.scheduler() or {}
        return {**s, "alive": alive(s.get("pid"))}

    @r.get("/state")
    async def state() -> dict:
        """The Tickets page: the tickets with their readiness and priority, the milestones, the
        counts by state, and whether the scheduler runs."""
        return {**store.ticket_views(), "scheduler": scheduler()}

    @r.post("/control")
    async def control(request: Request) -> dict:
        """Operator controls (not ticket editing): pause/resume new worker pickup, capacity, stop whoever works a ticket."""
        body = await request.json()
        action = body.get("action")
        if action in ("pause", "resume"):
            store.set_pickup_paused(action == "pause", by="operator")
            return {"ok": True, "pickup_paused": action == "pause"}
        if action == "capacity":
            # the operator's ceiling on workers and reviewers, live. Running turns finish;
            # only new pickups see the new number. Omit both to return to the configuration's
            try:
                w = int(body["workers"]) if body.get("workers") is not None else None
                rv = int(body["reviewers"]) if body.get("reviewers") is not None else None
            except (TypeError, ValueError):
                raise HTTPException(400, "workers and reviewers must be whole numbers")
            if (w is not None and not 0 <= w <= 64) or (rv is not None and not 0 <= rv <= 64):
                raise HTTPException(400, "capacity must be between 0 and 64")
            return {"ok": True, "capacity": store.set_operator_capacity(w, rv, by="operator")}
        if action == "kill":
            tid = body.get("ticket") or ""
            try:
                t = store.get_ticket(tid)
            except StoreError:
                raise HTTPException(404)
            if not any(a["ticket"] == tid for a in store.assignments().values()):
                raise HTTPException(409, f"{tid} is {t.state} and neither held nor offered; nothing to stop")
            store.request_kill(tid, by="operator")
            return {"ok": True, "ticket": tid}
        raise HTTPException(400, "unknown action")

    @r.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        """The task system's measurements in the Prometheus text format (tasking/measures.py)."""
        return store.metrics_text()

    @r.get("/tickets/{ticket_id}")
    async def ticket(ticket_id: str) -> dict:
        try:
            t = store.get_ticket(ticket_id)
        except StoreError:
            raise HTTPException(404)
        return {**t.to_dict(), "notes": store.notes(ticket_id)}

    @r.get("/attention")
    async def attention() -> dict:
        """What waits on a planner's decision: blocked tickets (a worker could not go on). Read on
        every page (static/attention.js)."""
        blocked = [{"id": t["id"], "reason": t.get("blocked_reason") or ""} for t in store.ticket_views()["tickets"] if t["state"] == "BLOCKED"]
        return {"ok": True, "blocked": blocked, "count": len(blocked)}

    @r.get("/board")
    async def board() -> dict:
        """The Tasks panel: every ticket in flight (not waiting to start, not finished; an offered
        one counts, and so does one whose offers nobody took), with the machine instance it is
        assigned to now (since when it holds it, or since when it was first offered, and what it
        last said it was doing) and the offers nobody took, blocked ones first,
        then the held, then the offered, then the rest by when they reached their state; and the
        queue by state, the capacity and the pause."""
        views = store.ticket_views()
        held = {a["ticket"]: a for a in store.assignments().values()}
        untaken = {r["ticket"]: r for r in store.untaken().values()}
        waiting = {"READY_FOR_REVIEW": "waiting for a reviewer", "REJECTED": "rejected: waiting for its worker",
                   "APPROVED": "approved", "BLOCKED": "blocked: waiting for a decision",
                   "IN_EXTERNAL_REVIEW": "in external review: waiting for its pull request"}
        rank = lambda t: (0 if t["state"] == "BLOCKED" else 1 if (held.get(t["id"]) or {}).get("instance")
                          else 2 if t["id"] in held or t["id"] in untaken else 3)
        flight = []
        for t in sorted(views["tickets"], key=lambda t: (rank(t), t.get("state_since") or "")):
            if t["state"] in ("OPEN", "DONE", "CANCELLED") and t["id"] not in held and t["id"] not in untaken:
                continue
            a, u = held.get(t["id"]), untaken.get(t["id"])
            flight.append({**{k: t.get(k) for k in ("id", "title", "state", "worker_turns", "reviewer_rejections", "gate_rejections", "pull_rejections", "blocked_reason")},
                           "waiting": None if a or u else waiting.get(t["state"], ""),
                           "assignment": ({"role": a["role"], "instance": a.get("instance"), "since": a.get("accepted_at") or a.get("offered_since"),
                                           "text": a.get("text") or ""} if a else None),
                           # offers of it nobody took: how many, since when, and when the next one goes out
                           "untaken": ({k: u[k] for k in ("role", "since", "misses", "again_at")} if u else None)})
        return {"ok": True, "now": time.time(), "tickets": flight, "counts": {k: v for k, v in views["counts"].items() if v}, "capacity": store.capacity(),
                "pickup_paused": store.pickup_paused(), "scheduler": scheduler()["alive"]}

    return r
