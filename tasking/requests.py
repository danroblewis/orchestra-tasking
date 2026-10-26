"""Requests: how a planner (the Supervisor, the Coordinator, a person or a script) asks the task
system to change its tickets and milestones, over Orchestra's bus.

A request is an event named for what it asks (``task.requested`` to create a ticket,
``task.unblock_requested``, ``milestone.close_requested`` …) with ``request`` (an id), ``args`` and
``by`` (the role asking, whose grants are checked). The scheduler's desk (``Desk``, in the
scheduler's process) checks and applies it with the rules it always had (``api.handle``) and
answers ``request.done`` with the result or ``request.refused`` with why. So every change to a
ticket is the task system's own, and Orchestra sees who asked for what
(docs/proposals/agents-split.md).

An agent's tool call sends its request this way when its environment says so
(``TASKING_REQUESTS=bus``, which the agents' sessions get) and waits for the answer; reads, and
what is the agents' own (their reports, the project context, escalations), are not requests."""

from __future__ import annotations

import os
import threading
import time
import uuid

from .assignments import Follower

REQUESTS = {"create_ticket": "task.requested", "update_ticket": "task.change_requested", "add_note": "task.note_requested",
            "cancel_ticket": "task.cancel_requested", "unblock_ticket": "task.unblock_requested",
            "create_milestone": "milestone.requested", "update_milestone": "milestone.change_requested",
            "close_milestone": "milestone.close_requested", "set_capacity": "capacity.change_requested",
            "resume_pipeline": "pipeline.resume_requested"}
OP_OF = {v: k for k, v in REQUESTS.items()}
DONE, REFUSED = "request.done", "request.refused"
WAIT_S = 30.0


def via_bus() -> bool:
    return os.environ.get("TASKING_REQUESTS") == "bus"


def _subject(args: dict) -> str | None:
    s = args.get("id") or args.get("milestone_id")
    return str(s) if s else None


def ask(project, command: str, args: dict, role: str) -> dict:
    """Send one request and wait for the task system's answer."""
    from .events import Events
    events = Events.for_project(project)
    follow = Follower(events)                       # from now: the answer comes after the request
    rid = uuid.uuid4().hex[:12]
    events.publish(REQUESTS[command], {"request": rid, "args": args, "by": role}, subject=_subject(args))
    end = time.time() + WAIT_S
    while time.time() < end:
        for ev in follow.new():
            if ev.get("request") == rid and ev.get("type") == DONE:
                return ev.get("result") or {"ok": True}
            if ev.get("request") == rid and ev.get("type") == REFUSED:
                return {"ok": False, "error": ev.get("error") or "refused"}
        time.sleep(0.25)
    return {"ok": False, "error": f"the task system did not answer within {int(WAIT_S)} s: is its scheduler (tasking run) running?"}


class Desk:
    """The scheduler's side: every request on the bus, checked and applied, and answered."""

    def __init__(self, project, store, events, log=print):
        self.project, self.store, self.events, self.log = project, store, events, log
        self.follow = Follower(events)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="requests")

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while not self.stop.wait(0.5):
            try:
                self.serve()
            except Exception as e:  # noqa: BLE001 - the desk stays open
                self.log(f"requests: {e!r}")

    def serve(self) -> None:
        from . import api
        for ev in self.follow.new():
            op = OP_OF.get(ev.get("type") or "")
            if not op:
                continue
            args, by = dict(ev.get("args") or {}), ev.get("by") or ""
            try:
                result = api.handle(self.project, self.store, op, args, role=by, direct=True)
            except Exception as e:  # noqa: BLE001 - a refused request is an answer, not a crash
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            ok = bool(result.get("ok", True))
            self.events.publish(DONE if ok else REFUSED, {"request": ev.get("request"), "op": op, "by": by,
                                                          **({"result": result} if ok else {"error": result.get("error")})},
                                subject=_subject(args) or (result.get("ticket") or {}).get("id"))
            self.log(f"request {op} by {by or 'someone'}: {'done' if ok else 'refused: ' + str(result.get('error'))[:80]}")
