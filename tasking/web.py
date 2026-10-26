"""The task system in Orchestra's web app (an Orchestra plugin, ``orchestra/plugins.py``): the
Tickets page (the ticket graph) and the Agents page, and the API they read, at
``/api/plugins/tasks``.

Lifted from Orchestra's web app as it was before the task system was removed (5006caf^). The
Supervisor chat and the operator controls (pause pickup, capacity, stop an agent) are the task
system's own write paths."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import psutil
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from orchestra.plugins import Declared, Page, Plugin

from .config import load_project
from .events import Events
from .harness import read_transcript
from .model import State
from .store import LocalStore, StoreError

STATIC = Path(__file__).parent / "static"
CHAT_PING_SECONDS = 20   # keep the chat stream alive through proxies while the Supervisor thinks
MARKER_TYPES = ("session_started", "turn_timed_out", "turn_error", "turn_interrupted", "agent_killed", "agent_crashed", "agent_crash_loop", "agent_reset", "agent_compacted")


def _alive(pid) -> bool:
    """A recorded pid proves liveness only if that process exists and is not a zombie."""
    try:
        return bool(pid) and psutil.Process(int(pid)).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


def plugin(orchestra_project) -> Plugin:
    """The plugin for the repository Orchestra serves (its entry point, in pyproject.toml)."""
    project = load_project(orchestra_project.repo_root)
    return Plugin("tasks", [Page("/tickets", "Tickets", STATIC / "tickets.html"), Page("/agents", "Agents", STATIC / "agents.html")],
                  STATIC, router(project), scripts=["attention.js", "machine-cards.js", "tasks-panel.js"], machines=declared(project))


def declared(project) -> list[Declared]:
    """The machines the task system provides, for Orchestra's Machines page before they run: the
    scheduler (a program) and every agent (an AI agent each)."""
    from .org import all_agents
    out = [Declared("task_scheduler", "program", "Assigns ready tickets to workers and reviewers, and starts the agents.", "continuous")]
    for a in all_agents(project).values():
        mode = {"continuous": "continuous", "scheduled": "scheduled", "ad_hoc": "ad hoc"}.get(a.kind, "ad hoc")
        out.append(Declared(a.name, "agent", a.description, mode, sorted({w.split(":")[0] for w in a.wake_on})))
    return out


def router(project) -> APIRouter:
    r = APIRouter()
    events = Events.for_project(project, source="tasks-web")
    store = LocalStore(project, events)

    @r.get("/roles")
    async def roles() -> dict:
        from . import api as api_mod
        return api_mod.handle(project, store, "list_roles", {})

    @r.get("/roles/{name}")
    async def role_detail(name: str) -> dict:
        from . import api as api_mod
        try:
            return api_mod.handle(project, store, "get_role", {"name": name})
        except StoreError:
            raise HTTPException(404)

    @r.get("/org")
    async def org() -> dict:
        """The organisation in one read: roles (job descriptions), agents (what runs), teams (who owns what)."""
        from . import api as api_mod
        return {"ok": True, "now": time.time(),
                "roles": api_mod.handle(project, store, "list_roles", {})["roles"],
                "agents": api_mod.handle(project, store, "list_agents", {})["agents"],
                "teams": api_mod.handle(project, store, "list_teams", {})["teams"]}

    @r.get("/agents/{name}")
    async def agent_detail(name: str) -> dict:
        from . import api as api_mod
        try:
            return api_mod.handle(project, store, "get_agent", {"name": name})
        except StoreError:
            raise HTTPException(404)

    @r.get("/teams/{name:path}")
    async def team_detail(name: str) -> dict:
        from . import api as api_mod
        try:
            return api_mod.handle(project, store, "get_team", {"name": name})
        except StoreError:
            raise HTTPException(404)

    @r.get("/state")
    async def state() -> dict:
        project.reload_config()
        snap = store.snapshot()
        snap["now"] = time.time()
        snap["gate_runs"] = store.gate_runs()[-30:]
        snap["batch_size"] = 1  # the gate takes one ticket per run until batching lands
        for s in snap["sessions"]:
            s["alive"] = _alive(s.get("pid"))
        try:
            from .org import all_agents
            snap["agents"] = [{"name": a.name, "session_id": a.session_id, "description": a.description, "builtin": a.builtin,
                               "team": a.team, "kind": a.kind, "resident": a.resident,
                               "paused": bool(store.agent_state(a.name).get("paused"))} for a in all_agents(project).values()]
        except Exception:
            snap["agents"] = []
        snap["loop"] = store.runtime().get("control") or {}
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

    @r.post("/supervisor")
    async def supervisor_message(request: Request):
        """The one write path in the UI: a human message to the resident Supervisor.
        Proxied to the control endpoint that `tasking run` serves; streams its NDJSON."""
        import urllib.request

        body = await request.json()
        ctl = store.runtime().get("control") or {}
        if not ctl.get("port"):
            raise HTTPException(503, "tasking run is not running; the Supervisor is only reachable while it is")
        req = urllib.request.Request(f"http://127.0.0.1:{ctl['port']}/supervisor", method="POST",
                                     data=__import__("json").dumps({"message": body.get("message", "")}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            # The control endpoint ends the stream when the turn ends; reads must not time out before that.
            resp = await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=max(project.config.turn_timeout or 0, 86400)))
        except Exception as e:
            raise HTTPException(503, f"cannot reach tasking run: {e}")

        async def gen():
            # Proxies in front of this page (Cloudflare closes a response that is silent for
            # 100s) must see bytes while the Supervisor thinks, so a ping goes out whenever
            # nothing has arrived for CHAT_PING_SECONDS. The client ignores pings.
            q: asyncio.Queue[bytes | None] = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def pump():
                try:
                    while True:
                        line = resp.readline()
                        loop.call_soon_threadsafe(q.put_nowait, line or None)
                        if not line:
                            return
                except Exception:
                    loop.call_soon_threadsafe(q.put_nowait, None)
            threading.Thread(target=pump, daemon=True).start()
            while True:
                try:
                    line = await asyncio.wait_for(q.get(), timeout=CHAT_PING_SECONDS)
                except TimeoutError:
                    yield b'{"type": "ping"}\n'
                    continue
                if line is None:
                    break
                yield line

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @r.get("/context")
    async def context() -> dict:
        from . import agents
        return {"text": agents.read_context(project)}

    @r.post("/control")
    async def control(request: Request) -> dict:
        """Operator controls (not ticket editing): pause/resume new worker pickup, stop an agent."""
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
        if action in ("pause_agent", "resume_agent", "restart_agent", "reset_agent", "delete_agent", "delete_role", "delete_team"):
            from . import api as api_mod
            try:
                return api_mod.handle(project, store, action, {"name": body.get("name") or ""}, role="supervisor")
            except StoreError as e:
                raise HTTPException(409, str(e))
        raise HTTPException(400, "unknown action")

    @r.get("/tickets/{ticket_id}")
    async def ticket(ticket_id: str) -> dict:
        try:
            t = store.get_ticket(ticket_id)
        except StoreError:
            raise HTTPException(404)
        return {**t.to_dict(), "notes": store.notes(ticket_id), "worktree": store.worktree(ticket_id)}

    @r.get("/sessions/{session_id}/transcript")
    async def transcript(session_id: str, before: int = 0, limit: int = 30, after: int | None = None) -> dict:
        """A page of a session's transcript, newest first. `before` counts messages back from
        the end that are already shown; the page returns the next `limit` older ones. `after`
        instead returns every message past the first `after` ones, which is how an open
        drawer polls for what arrived since it was rendered (transcripts only grow)."""
        s = store.find_session(session_id)
        if not s:
            raise HTTPException(404)
        f = s.get("session_file")
        exists = bool(f) and Path(f).exists()
        msgs = read_transcript(s) if exists else []
        total = len(msgs)
        if after is not None:
            page, end = msgs[max(0, after):], total
        else:
            end = max(0, total - max(0, before))
            page = msgs[max(0, end - max(1, min(limit, 200))):end]
        page.reverse()
        s = {**s, "alive": _alive(s.get("pid")), "file_exists": exists}
        pf = project.logs_dir / "agents" / f"{session_id}.prompt.md"
        started_with = pf.read_text(encoding="utf-8") if pf.exists() else None
        # What Orchestra did to this session: turn starts, cut-offs, errors, operator stops.
        # The session file stays the only transcript; the view overlays the event log.
        markers = [e for e in events.read(0, limit=100000)[0]
                   if e.get("session") == session_id and e["type"] in MARKER_TYPES][-50:]
        return {"session": s, "messages": page, "total": total, "before": before, "next_before": total - (end - len(page)) if page else before,
                "has_more": end - len(page) > 0, "turns": sum(1 for m in msgs if m["role"] == "user"), "started_with": started_with,
                "markers": markers}

    @r.get("/gate/runs/{run_id}")
    async def gate_run(run_id: str) -> dict:
        for r in store.gate_runs():
            if r["id"] == run_id:
                return r
        raise HTTPException(404)

    @r.get("/attention")
    async def attention() -> dict:
        """What waits on the human: blocked tickets (a worker could not go on; usually escalated
        through the Coordinator to the Supervisor) and Supervisor messages written since the
        human last read its chat. Read on every page (static/attention.js)."""
        snap = store.snapshot()
        blocked = [{"id": t["id"], "title": t["title"], "reason": t.get("blocked_reason") or "", "since": t.get("state_since"),
                    "milestone": t.get("milestone_id")} for t in snap["tickets"] if t["state"] == "BLOCKED"]
        s = store.find_session("supervisor") or {}
        seen = float(store.runtime().get("supervisor_seen_at") or 0)
        settled = float(s.get("settled_at") or 0)
        message = {"text": s.get("last_text") or "", "at": settled} if settled > seen and s.get("last_text") else None
        return {"ok": True, "blocked": blocked, "message": message, "working": s.get("status") == "running",
                "count": len(blocked) + (1 if message else 0)}

    @r.post("/attention/seen")
    async def attention_seen() -> dict:
        """The human has read the Supervisor's chat up to now."""
        now_t = time.time()
        store._mutate_runtime(lambda d: d.__setitem__("supervisor_seen_at", now_t))
        return {"ok": True, "seen_at": now_t}

    @r.get("/eventmap")
    async def eventmap() -> dict:
        """Who wakes whom through which events (the Agents page's map): the intended bindings
        (each agent's wake_on and emits, team emits, probes, the built-in loop's own conditions)
        and what was observed on the bus over the last 24 hours, by the machine that sent it."""
        import datetime as _dt

        from .agents.roles import SYSTEM_BINDINGS
        from .org import all_agents, all_teams
        from .roles import EVENT_TYPES
        agents_, teams = all_agents(project), all_teams(project)
        nodes: dict[str, dict] = {}
        edges: list[dict] = []

        def node(nid, kind, label=None, **extra):
            nodes.setdefault(nid, {"id": nid, "kind": kind, "label": label or nid, **extra})

        def split(w):
            t, _, f = w.partition(":")
            return t, f or None
        for sid, sb in SYSTEM_BINDINGS.items():
            if sid == "merge_gate" and project.config.work != "code":
                continue                                          # tasks: nothing is merged
            node("sys:" + sid, "system", sid.replace("_", " "), description=sb["description"])
            for e in sb["emits"]:
                t, f = split(e)
                node("ev:" + t, "event", t)
                edges.append({"from": "sys:" + sid, "to": "ev:" + t, "kind": "emits", "filter": f})
            for w in sb["wake_on"]:
                t, f = split(w)
                node("ev:" + t, "event", t)
                edges.append({"from": "ev:" + t, "to": "sys:" + sid, "kind": "wakes", "filter": f})
        for t in teams.values():
            for e in t.emits:
                node("ev:" + e, "event", e, custom=True, team=t.name)
            for pr in t.probes or []:
                node("probe:" + pr["name"], "probe", pr["name"], team=t.name)
                for e in ("probe_failed", "probe_recovered"):
                    node("ev:" + e, "event", e)
                    edges.append({"from": "probe:" + pr["name"], "to": "ev:" + e, "kind": "emits", "filter": pr["name"]})
        for ag in agents_.values():
            node("agent:" + ag.name, "agent", ag.name, team=ag.team, builtin=ag.builtin, kind_of=ag.kind)
            for w in ag.wake_on:
                t, f = split(w)
                node("ev:" + t, "event", t)
                edges.append({"from": "ev:" + t, "to": "agent:" + ag.name, "kind": "wakes", "filter": f, "loop": ag.builtin})
            for e in ag.emits:
                t, f = split(e)
                node("ev:" + t, "event", t)
                edges.append({"from": "agent:" + ag.name, "to": "ev:" + t, "kind": "emits", "filter": f})
            if ag.team and ag.team in teams:
                for e in teams[ag.team].emits:
                    edges.append({"from": "agent:" + ag.name, "to": "ev:" + e, "kind": "may_emit", "filter": None})
        since = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)).isoformat(timespec="seconds")
        counts: dict[str, int] = {}
        by: dict[str, dict[str, int]] = {}
        end = events.end()
        for e in events.read(max(0, end - 200000), limit=200000)[0]:
            if (e.get("ts") or "") < since:
                continue
            counts[e["type"]] = counts.get(e["type"], 0) + 1
            variant = e.get("to") or e.get("probe")
            if variant:
                counts[f"{e['type']}:{variant}"] = counts.get(f"{e['type']}:{variant}", 0) + 1
            who = (e.get("source") or "").partition("/")[0]           # the machine that sent it (worker/1 is worker)
            if who:
                by.setdefault(who, {})
                by[who][e["type"]] = by[who].get(e["type"], 0) + 1
        for who, ts_ in by.items():
            if who in agents_:
                for t, n in ts_.items():
                    node("ev:" + t, "event", t)
                    hits = [x for x in edges if x["from"] == "agent:" + who and x["to"] == "ev:" + t]
                    if hits:
                        for x in hits:
                            x["seen"] = n                      # a declared edge that actually fired
                    else:
                        edges.append({"from": "agent:" + who, "to": "ev:" + t, "kind": "observed", "filter": None, "seen": n})
        return {"ok": True, "nodes": list(nodes.values()), "edges": edges, "counts": counts, "by": by,
                "known_types": list(EVENT_TYPES)}

    @r.get("/board")
    async def board() -> dict:
        """The Tasks panel: every ticket in flight (not waiting to start, not finished), with the
        machine instance that owns it now and that instance's session, newest activity first; and
        the queue by state, the capacity and the pause."""
        snap = store.snapshot()
        running = {s.get("ticket_id"): s for s in snap["sessions"] if s.get("status") == "running" and s.get("ticket_id")}
        waiting = {"READY_FOR_REVIEW": "waiting for a reviewer", "REJECTED": "rejected: waiting for its worker",
                   "APPROVED": "approved", "BLOCKED": "blocked: waiting for a decision"}
        flight = []
        for t in snap["tickets"]:
            if t["state"] in ("OPEN", "DONE", "CANCELLED"):
                continue
            s = running.get(t["id"])
            flight.append({**{k: t.get(k) for k in ("id", "title", "state", "owner", "state_since", "worker_turns", "reviewer_rejections",
                                                       "gate_rejections", "blocked_reason", "milestone_id")},
                           "waiting": None if s else waiting.get(t["state"], ""),
                           "session": ({**{k: v for k, v in s.items() if k not in ("partial_text", "last_text")}, "alive": _alive(s.get("pid")),
                                        "text": (s.get("partial_text") or s.get("last_text") or "")[-300:]} if s else None)})
        flight.sort(key=lambda x: (x["session"] is None, x["state_since"] or ""), reverse=False)
        counts: dict[str, int] = {}
        for t in snap["tickets"]:
            counts[t["state"]] = counts.get(t["state"], 0) + 1
        loop = store.runtime().get("control") or {}
        return {"ok": True, "now": time.time(), "tickets": flight, "counts": counts, "capacity": store.capacity(),
                "pickup_paused": store.pickup_paused(), "scheduler": _alive(loop.get("pid"))}

    @r.get("/agents")
    async def agents_view() -> dict:
        """Every agent the task system runs, with what it is doing now: its running sessions."""
        from .org import all_agents
        snap = store.snapshot()
        out = []
        for a in all_agents(project).values():
            mine = [s for s in snap["sessions"] if s["id"] == a.session_id or s.get("kind") == a.name or s["id"].startswith(a.name + "-")]
            jobs = [{"session": s["id"], "ticket": s.get("ticket"), "text": (s.get("partial_text") or s.get("last_text") or "")[-160:]}
                    for s in mine if s.get("status") == "running"]
            alive = any(_alive(s.get("pid")) for s in mine)
            out.append({"id": a.name, "name": a.name, "kind": "agent", "description": a.description, "team": a.team, "session_id": a.session_id,
                        "resident": a.resident, "paused": bool(store.agent_state(a.name).get("paused")),
                        "status": "working" if jobs else ("waiting" if alive else "not running"), "jobs": jobs})
        loop = store.runtime().get("control") or {}
        # every agent session as the machine instance it runs (or last ran) as, with its ticket
        tickets = {t["id"]: t for t in snap["tickets"]}
        loop_alive = _alive(loop.get("pid"))
        # a resident agent between turns: on Pi a live process; on Claude Code or Codex there is no
        # process between turns, and it is up as long as the scheduler that runs it is
        residents = {a["session_id"] for a in out if a.get("resident")}
        insts = []
        for s in snap["sessions"]:
            t = tickets.get(s.get("ticket_id") or "")
            insts.append({**{k: v for k, v in s.items() if k not in ("partial_text", "last_text")},       # what sessionChip reads
                          "session": s["id"], "instance": s.get("instance") or s.get("kind"), "role": s.get("kind"),
                          "alive": _alive(s.get("pid")) or (s["id"] in residents and s.get("status") != "running" and loop_alive),
                          "tool": s.get("tool_name"),
                          "last_text": (s.get("partial_text") or s.get("last_text") or "")[-300:],
                          "ticket": {k: t.get(k) for k in ("id", "title", "state", "worker_turns", "reviewer_rejections", "gate_rejections")} if t else None})
        counts = {}
        for t in snap["tickets"]:
            counts[t["state"]] = counts.get(t["state"], 0) + 1
        return {"ok": True, "loop": {**loop, "alive": loop_alive}, "agents": out, "sessions": insts,
                "tickets": counts, "capacity": store.capacity(), "pickup_paused": store.pickup_paused(),
                "harness": project.config.harness, "model": project.config.model, "now": time.time()}

    return r
