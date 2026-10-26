"""The task system's operations on tickets, milestones, capacity, the gate and staging: what a
planner's request asks for (tasking/requests.py), applied by the scheduler's desk. ``role`` is
the label the asker chose (``by``); it names the author of what it makes. Which planner may change
what is the planners' own organisation, not the task system's: it checks the rules of tickets and
milestones (section 2), not who asks."""

from __future__ import annotations

from typing import Any

from .config import Project
from .events import Events
from .measures import machine_of
from .model import ACTIVE, TERMINAL, Note, State
from .staging import Staging
from .store import LocalStore, StoreError


def _ticket_view(store: LocalStore, ticket_id: str) -> dict:
    t = store.get_ticket(ticket_id).to_dict()
    t["notes"] = store.notes(ticket_id)
    return t


def handle(project: Project, store: LocalStore, events: Events, command: str, args: dict[str, Any], role: str = "") -> dict:
    a = args or {}
    author = role

    if command == "project_status":
        snap = store.snapshot()
        return {"ok": True, "counts": snap["counts"], "pipeline": snap["pipeline"],
                "milestones": [{"id": m["id"], "name": m["name"], "status": m["status"]} for m in snap["milestones"]],
                "assigned": [{"ticket": a["ticket"], "role": a["role"], "instance": a.get("instance")} for a in store.assignments().values()],
                "blocked": [{"id": t["id"], "reason": t["blocked_reason"]} for t in snap["tickets"] if t["state"] == "BLOCKED"]}

    if command == "snapshot":
        return {"ok": True, "snapshot": store.snapshot()}

    if command == "list_milestones":
        tickets = store.list_tickets()
        out = []
        for m in store.list_milestones():
            mine = [t for t in tickets if t.milestone_id == m.id]
            out.append({**m.to_dict(), "tickets": len(mine),
                        "done": sum(1 for t in mine if t.state == State.DONE)})
        return {"ok": True, "milestones": out}

    if command == "create_milestone":
        m = store.create_milestone(name=a["name"], description=a.get("description", ""))
        return {"ok": True, "milestone": m.to_dict()}

    if command == "update_milestone":
        changes = {k: a[k] for k in ("name", "description", "order") if k in a and a[k] is not None}
        return {"ok": True, "milestone": store.update_milestone(a["id"], None, **changes).to_dict()}

    if command == "close_milestone":
        return {"ok": True, "milestone": store.close_milestone(a["id"], None).to_dict()}

    if command == "list_tickets":
        state = State(a["state"]) if a.get("state") else None
        ts = store.list_tickets(state=state, milestone_id=a.get("milestone_id") or None)
        return {"ok": True, "tickets": [
            {"id": t.id, "title": t.title, "state": t.state.value, "milestone_id": t.milestone_id,
             "priority": t.priority, "depends_on": t.depends_on, "blocked_reason": t.blocked_reason,
             "review_rounds": t.review_rounds, "objective": t.objective} for t in ts]}

    if command == "get_ticket":
        return {"ok": True, "ticket": _ticket_view(store, a["id"])}

    if command == "create_ticket":
        t = store.create_ticket(
            by=machine_of(author), title=a["title"], description=a.get("description", ""), acceptance=a.get("acceptance", ""),
            objective=a.get("objective", ""), verification=a.get("verification", ""),
            milestone_id=a["milestone_id"], priority=int(a.get("priority") or 5),
            depends_on=list(a.get("depends_on") or []), created_by=author,
        )
        return {"ok": True, "ticket": t.to_dict()}

    if command == "update_ticket":
        changes = {k: a[k] for k in ("title", "description", "acceptance", "objective", "verification",
                                     "priority", "milestone_id", "depends_on") if k in a and a[k] is not None}
        return {"ok": True, "ticket": store.update_ticket(a["id"], None, **changes).to_dict()}

    if command == "add_note":
        store.add_note(a["id"], Note(author=author, text=a["text"]))
        return {"ok": True}

    if command == "cancel_ticket":
        t = store.get_ticket(a["id"])
        if t.state in TERMINAL:
            raise StoreError(f"{t.id} is already {t.state}")
        if t.state in ACTIVE:
            raise StoreError(f"{t.id} is {t.state}: someone holds it; wait for it to settle, then cancel")
        store.add_note(t.id, Note(author=author, text=f"cancelled: {a.get('reason', '')}"))
        t = store.transition(t.id, t.version, State.CANCELLED, by=machine_of(author))
        return {"ok": True, "ticket": t.to_dict()}

    if command == "unblock_ticket":
        t = store.get_ticket(a["id"])
        if t.state != State.BLOCKED:
            raise StoreError(f"{t.id} is {t.state}, not BLOCKED")
        store.add_note(t.id, Note(author=author, text=f"unblocked: {a.get('note', '')}"))
        t = store.transition(t.id, t.version, State.OPEN, by=machine_of(author), blocked_reason=None, rejected_by=None,   # the planner's word replaces the rejection's
                             feedback=f"the blocker was resolved by {author}: {a.get('note', '')}")
        return {"ok": True, "ticket": t.to_dict()}

    if command == "get_capacity":
        return {"ok": True, "capacity": store.capacity()}

    if command == "set_capacity":
        cap = store.set_capacity(a.get("workers"), a.get("reviewers"), note=a.get("note", ""), by=author)
        return {"ok": True, "capacity": cap,
                "message": f"effective workers={cap['workers']} (max {cap['max_workers']}), reviewers={cap['reviewers']} (max {cap['max_reviewers']})"}

    if command == "list_gate_runs":
        return {"ok": True, "runs": [{k: r.get(k) for k in ("id", "status", "ticket", "started_at", "finished_at", "summary")}
                                     for r in store.gate_runs()[-20:]]}

    if command == "get_gate_run":
        for r in store.gate_runs():
            if r["id"] == a["id"]:
                return {"ok": True, "run": r}
        raise StoreError(f"no gate run {a['id']}")

    if command == "get_staging_status":
        st = Staging(project, store, events)
        return {"ok": True, "staging": st.state(), "log_tail": st.logs(3000)}

    if command == "resume_pipeline":
        store.set_pipeline(False, None)
        return {"ok": True}

    raise StoreError(f"unknown command {command}")
