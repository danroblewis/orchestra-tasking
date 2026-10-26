"""Requests: how a planner (a person's script, a program, a machine from another package: whatever
decides what the work is) asks the task system to change its tickets and
milestones, over Orchestra's bus.

A request is an event named for what it asks (``task.requested`` to create a ticket,
``task.unblock_requested``, ``milestone.close_requested`` …) with ``request`` (an id), ``args`` and
``by`` (the label the asker chooses; it names the author of what it makes).
Reads are one event type, ``task.query_requested``, with the operation in ``op``. The scheduler's
desk (``Desk``, in the scheduler's process) checks and applies each with the task system's rules
(``api.handle``) and answers ``request.done`` with the result or ``request.refused`` with why. So
every change to a ticket is the task system's own, and Orchestra sees who asked for what.

The desk reads on from where the last one stopped (the offset and the ids it answered from there,
kept in the runtime record), so a request made while no scheduler ran is answered when one
starts: applied if its asker may still be waiting (``RESUME_S``, twice what ``ask`` waits),
refused with the reason if older, since applying a change long after its asker gave up would
surprise everyone. A request is never applied twice: the ids answered since the saved offset are
saved with it."""

from __future__ import annotations

import threading
import time
import uuid

from . import api
from .assignments import Follower
from .model import epoch

REQUESTS = {"create_ticket": "task.requested", "update_ticket": "task.change_requested", "add_note": "task.note_requested",
            "cancel_ticket": "task.cancel_requested", "unblock_ticket": "task.unblock_requested",
            "create_milestone": "milestone.requested", "update_milestone": "milestone.change_requested",
            "close_milestone": "milestone.close_requested", "set_capacity": "capacity.change_requested",
            "resume_pipeline": "pipeline.resume_requested"}
OP_OF = {v: k for k, v in REQUESTS.items()}
QUERY = "task.query_requested"
QUERIES = ("list_tickets", "get_ticket", "project_status", "list_milestones", "get_capacity", "snapshot",
           "list_gate_runs", "get_gate_run", "get_staging_status")
DONE, REFUSED = "request.done", "request.refused"
WAIT_S = 30.0            # how long an asker waits for the answer
RESUME_S = 2 * WAIT_S    # a request older than this when the desk reads it is refused, not applied


def _age(ev: dict) -> float:
    """Seconds since the event was published (its CloudEvents time), 0 when it says none."""
    published = epoch(ev.get("ts"))
    return 0.0 if published is None else max(0.0, time.time() - published)


def _subject(args: dict) -> str | None:
    s = args.get("id") or args.get("milestone_id")
    return str(s) if s else None


def ask(events, command: str, args: dict, by: str) -> dict:
    """Send one request as ``by`` and wait for the task system's answer. ``events`` is the
    asker's (its source names the machine asking)."""
    follow = Follower(events)                       # from now: the answer comes after the request
    rid = uuid.uuid4().hex[:12]
    if command in QUERIES:
        events.publish(QUERY, {"request": rid, "op": command, "args": args, "by": by}, subject=_subject(args))
    elif command in REQUESTS:
        events.publish(REQUESTS[command], {"request": rid, "args": args, "by": by}, subject=_subject(args))
    else:
        return {"ok": False, "error": f"the task system has no operation {command!r}; changes: {', '.join(REQUESTS)}; queries: {', '.join(QUERIES)}"}
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
    """The scheduler's side: every request on the bus since the last one answered, checked and
    applied, and answered."""

    def __init__(self, project, store, events, log=print):
        self.project, self.store, self.events, self.log = project, store, events, log
        rec = store.desk() or {}
        start = min(int(rec.get("offset", 0)), events.end()) if rec else None     # a stream shorter than the record: from its end
        self.follow = Follower(events, start=start)
        self.answered: list[str] = list(rec.get("answered") or [])   # answered from ``start`` on before this desk started
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
        """One batch of new events. The offset is saved after the batch and, while it is being
        answered, the ids answered from the batch's start, so a desk that stops halfway (a crash)
        picks up without answering any request twice."""
        start, served = self.follow.offset, False
        for ev in self.follow.new():
            query = ev.get("type") == QUERY
            op = ev.get("op") if query else OP_OF.get(ev.get("type") or "")
            if (not query and not op) or ev.get("request") in self.answered:
                continue
            self._answer(ev, op, query)
            self.answered.append(ev.get("request"))
            self.store.set_desk(start, self.answered)
            served = True
        if served:
            self.answered = []
            self.store.set_desk(self.follow.offset, [])

    def _answer(self, ev: dict, op: str, query: bool) -> None:
        args, by, age = dict(ev.get("args") or {}), ev.get("by") or "", _age(ev)
        if age > RESUME_S:
            result = {"ok": False, "error": f"no scheduler was running when this was asked, {int(age)} s ago, longer than an asker "
                                           f"waits ({int(WAIT_S)} s); not applied now: ask again if it still holds"}
        elif query and op not in QUERIES:
            result = {"ok": False, "error": f"no query op {op!r}; queries: {', '.join(QUERIES)}"}
        elif op in REQUESTS and not by:
            result = {"ok": False, "error": "a request names who asks (by)"}
        else:
            try:
                result = api.handle(self.project, self.store, self.events, op, args, role=by)
            except Exception as e:  # noqa: BLE001 - a refused request is an answer, not a crash
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        ok = bool(result.get("ok", True))
        self.events.publish(DONE if ok else REFUSED, {"request": ev.get("request"),
                                                      **({"result": result} if ok else {"error": result.get("error")})},
                            subject=_subject(args) or (result.get("ticket") or {}).get("id"))
        if op not in QUERIES or age > RESUME_S:
            self.log(f"request {op} by {by or 'someone'}: {'done' if ok else 'refused: ' + str(result.get('error'))[:80]}")
