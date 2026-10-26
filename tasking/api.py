"""Service functions behind `tasking api`, the CLI, and the web server.

Every write an agent can make goes through `handle()`. The role and ticket come from the
environment Orchestra set when it launched the agent's Pi process, never from the agent."""

from __future__ import annotations

from typing import Any

from .api_org import ORG_COMMANDS, handle_org
from .config import Project
from .model import ACTIVE, TERMINAL, Note, State
from .store import LocalStore, StoreError

AGENT_ROLES = {"supervisor", "coordinator"}


def _has_tools(project: Project, role: str, group: str) -> bool:
    """A custom agent may do what its roles and team grant it (the built-ins are listed in
    AGENT_ROLES); the grant is the same one the harness started it with."""
    from .org import all_agents, all_teams, effective_tools
    from .roles import all_roles
    ag = all_agents(project).get(role)
    return bool(ag) and group in effective_tools(ag, all_roles(project), all_teams(project))


def _ticket_view(store: LocalStore, ticket_id: str) -> dict:
    t = store.get_ticket(ticket_id).to_dict()
    t["notes"] = store.notes(ticket_id)
    return t


def handle(project: Project, store: LocalStore, command: str, args: dict[str, Any],
           role: str = "", ticket_env: str = "", direct: bool = False) -> dict:
    a = args or {}
    author = role or "orchestra"
    # An agent's change to tickets or milestones is a request to the task system, over the bus
    # (tasking/requests.py); the scheduler's desk applies it here, with direct=True.
    from . import requests
    if not direct and command in requests.REQUESTS and requests.via_bus():
        return requests.ask(project, command, a, role)

    if command == "project_status":
        snap = store.snapshot()
        return {"ok": True, "counts": snap["counts"], "pipeline": snap["pipeline"],
                "milestones": [{"id": m["id"], "name": m["name"], "status": m["status"]} for m in snap["milestones"]],
                "active_sessions": [s["id"] for s in snap["sessions"] if s.get("status") == "running"],
                "blocked": [{"id": t["id"], "reason": t["blocked_reason"]} for t in snap["tickets"] if t["state"] == "BLOCKED"]}

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
        store.add_wakeup("coordinator", f"milestone {m.id} created by {author}")
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
        if role and role not in AGENT_ROLES and not _has_tools(project, role, "tickets"):
            raise StoreError(f"role {role} may not create tickets")
        t = store.create_ticket(
            title=a["title"], description=a.get("description", ""), acceptance=a.get("acceptance", ""),
            objective=a.get("objective", ""), verification=a.get("verification", ""),
            milestone_id=a["milestone_id"], priority=int(a.get("priority") or 5),
            depends_on=list(a.get("depends_on") or []), created_by=author,
        )
        if author != "coordinator":
            store.add_wakeup("coordinator", f"ticket {t.id} created by {author}: {t.title}")
        return {"ok": True, "ticket": t.to_dict()}

    if command == "update_ticket":
        changes = {k: a[k] for k in ("title", "description", "acceptance", "objective", "verification",
                                     "priority", "milestone_id", "depends_on") if k in a and a[k] is not None}
        if role == "coordinator" and "objective" in changes:
            raise StoreError("the coordinator may not change a ticket's objective; use escalate_to_supervisor")
        return {"ok": True, "ticket": store.update_ticket(a["id"], None, **changes).to_dict()}

    if command == "add_note":
        store.add_note(a["id"], Note(author=author, text=a["text"]))
        return {"ok": True}

    if command == "cancel_ticket":
        t = store.get_ticket(a["id"])
        if t.state in TERMINAL:
            raise StoreError(f"{t.id} is already {t.state}")
        if t.state in ACTIVE:
            raise StoreError(f"{t.id} is {t.state} with a live agent; wait for it to settle, then cancel")
        store.add_note(t.id, Note(author=author, text=f"cancelled: {a.get('reason', '')}"))
        t = store.transition(t.id, t.version, State.CANCELLED)
        return {"ok": True, "ticket": t.to_dict()}

    if command == "unblock_ticket":
        t = store.get_ticket(a["id"])
        if t.state != State.BLOCKED:
            raise StoreError(f"{t.id} is {t.state}, not BLOCKED")
        store.add_note(t.id, Note(author=author, text=f"unblocked: {a.get('note', '')}"))
        t = store.transition(t.id, t.version, State.OPEN, blocked_reason=None,
                             feedback=f"Blocker resolved by {author}: {a.get('note', '')}")
        return {"ok": True, "ticket": t.to_dict()}

    if command == "report_ticket":
        if role != "worker":
            raise StoreError("only workers may call report_ticket")
        t = store.get_ticket(ticket_env)
        if t.state != State.IN_PROGRESS:
            raise StoreError(f"{t.id} is {t.state}; report ignored")
        state = a.get("state")
        if state not in ("READY_FOR_REVIEW", "BLOCKED"):
            raise StoreError("state must be READY_FOR_REVIEW or BLOCKED")
        if state == "BLOCKED" and not (a.get("blocked_reason") or "").strip():
            raise StoreError("BLOCKED requires blocked_reason")
        store.set_report(t.id, {"kind": "worker", "state": state, "summary": a.get("summary", ""),
                                "blocked_reason": a.get("blocked_reason")})
        return {"ok": True, "message": "Report recorded. You may stop now."}

    if command == "report_review":
        if role != "reviewer":
            raise StoreError("only reviewers may call report_review")
        t = store.get_ticket(ticket_env)
        if t.state != State.IN_REVIEW:
            raise StoreError(f"{t.id} is {t.state}; report ignored")
        verdict = a.get("verdict")
        if verdict not in ("APPROVED", "REJECTED"):
            raise StoreError("verdict must be APPROVED or REJECTED")
        if verdict == "REJECTED" and not (a.get("feedback") or "").strip():
            raise StoreError("REJECTED requires feedback")
        store.set_report(t.id, {"kind": "reviewer", "verdict": verdict, "summary": a.get("summary", ""),
                                "feedback": a.get("feedback")})
        return {"ok": True, "message": "Verdict recorded. You may stop now."}

    if command == "get_project_context":
        from . import agents
        return {"ok": True, "context": agents.read_context(project)}

    if command == "update_project_context":
        from . import agents
        if role and role != "supervisor":
            raise StoreError("only the supervisor maintains the project context")
        text = a.get("text") or ""
        if len(text.strip()) < 20:
            raise StoreError("context text is too short; write the full document")
        agents.write_context(project, text)
        store.events.emit("context_updated", by=author)
        return {"ok": True}

    if command in ORG_COMMANDS:
        return handle_org(project, store, command, a, author, role)

    if command == "read_events":
        from datetime import datetime, timezone
        minutes = int(a.get("minutes") or 60)
        types = set(a.get("types") or [])
        since = __import__("time").time() - minutes * 60
        evs, _ = store.events.read(0, limit=5000)
        out = []
        for e in evs:
            try:
                ts = datetime.fromisoformat(e["ts"]).replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                continue
            if ts < since or (types and e.get("type") not in types):
                continue
            if e.get("type") in ("session_started", "session_settled"):
                continue
            out.append(e)
        return {"ok": True, "events": out[-200:], "count": len(out)}

    if command == "get_capacity":
        return {"ok": True, "capacity": store.capacity()}

    if command == "set_capacity":
        if role and role not in ("coordinator", "supervisor"):
            raise StoreError("only the coordinator or supervisor may change capacity")
        cap = store.set_capacity(a.get("workers"), a.get("reviewers"), note=a.get("note", ""), by=author)
        return {"ok": True, "capacity": cap,
                "message": f"effective workers={cap['workers']} (max {cap['max_workers']}), reviewers={cap['reviewers']} (max {cap['max_reviewers']})"}

    if command == "escalate_to_supervisor":
        store.add_wakeup("supervisor", f"[{author}] {a.get('note', '')}")
        return {"ok": True, "message": "The supervisor will be woken with your note."}

    if command == "list_gate_runs":
        return {"ok": True, "runs": [{k: r.get(k) for k in ("id", "status", "batch", "started_at", "finished_at", "summary")}
                                     for r in store.gate_runs()[-20:]]}

    if command == "get_gate_run":
        for r in store.gate_runs():
            if r["id"] == a["id"]:
                return {"ok": True, "run": r}
        raise StoreError(f"no gate run {a['id']}")

    if command == "get_staging_status":
        from .staging import Staging
        st = Staging(project, store, store.events)
        return {"ok": True, "staging": st.state(), "log_tail": st.logs(3000)}

    if command == "restart_staging":
        from .staging import Staging
        return {"ok": True, "result": Staging(project, store, store.events).restart()}

    if command == "resume_pipeline":
        store.set_pipeline(False, None)
        return {"ok": True}

    raise StoreError(f"unknown command {command}")
