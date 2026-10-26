"""One-screen status: what needs a decision, the scheduler, who holds work, the queue; in code
mode the merge gate and the pipeline; staging when it is configured."""

from __future__ import annotations

import time
from datetime import datetime

from .config import Project
from .events import Events
from .model import State
from .procs import alive
from .staging import Staging
from .store import LocalStore


def gate_stage_plan(project: Project) -> int:
    cfg = project.config
    n = len(cfg.unit) + (1 if cfg.build_command else 0)
    if cfg.acceptance and cfg.acceptance_start:
        n += 1 + len(cfg.acceptance)
    return max(n, 1)


def collect(project: Project, store: LocalStore, events: Events) -> dict:
    cfg = project.config
    now = time.time()
    tickets = store.list_tickets()
    by_id = {t.id: t for t in tickets}

    sch = store.scheduler() or {}
    workers = [{"id": a.get("instance") or f"(offered to a {a['role']})", "kind": a["role"], "ticket": a["ticket"],
                "title": by_id[a["ticket"]].title if a["ticket"] in by_id else "",
                "elapsed": round(now - (a.get("assigned_at") or now))}
               for a in store.assignments().values()]

    runs = store.gate_runs()
    cur = next((r for r in runs if r.get("status") == "running"), None)
    gate = None
    if cur:
        total = gate_stage_plan(project)
        done = len(cur.get("stages", []))
        failed = any(s.get("status") == "failed" for s in cur.get("stages", []))
        started = cur.get("started_at")
        gate = {"id": cur["id"], "tickets": [{"id": t, "title": by_id[t].title if t in by_id else ""} for t in [cur["ticket"]]],
                "stages_done": done, "stages_total": total, "percent": int(100 * min(done, total) / total),
                "failed_stage": failed, "current_stage": (cur["stages"][-1]["stage"] + ":" + cur["stages"][-1]["name"]) if cur.get("stages") else "assembling",
                "started_at": started}
    last = next((r for r in reversed(runs) if r.get("status") != "running"), None)
    approved = sorted((t for t in tickets if t.state == State.APPROVED), key=lambda t: t.state_since)
    on_the_way = [t for t in tickets if t.state in (State.IN_REVIEW, State.READY_FOR_REVIEW, State.REJECTED, State.IN_PROGRESS)]

    st = Staging(project, store, events)
    sstate = st.state() if cfg.staging_configured else {"configured": False}
    staging = {"configured": cfg.staging_configured, "status": sstate.get("status"), "alive": sstate.get("alive"),
               "build": sstate.get("current_build_id"), "local_url": sstate.get("local_url"),
               "public_url": sstate.get("public_url"), "deployed_at": sstate.get("deployed_at"),
               "log_tail": st.logs(4000).strip().splitlines()[-6:] if cfg.staging_configured else []}

    counts = {s.value: sum(1 for t in tickets if t.state == s) for s in State}
    return {
        "project": {"key": project.key, "repo_root": str(project.repo_root), "base_branch": cfg.base_branch, "work": cfg.work},
        "scheduler": {"running": alive(sch.get("pid")), "pid": sch.get("pid")},
        "blocked": [{"id": t.id, "title": t.title, "reason": (t.blocked_reason or "").splitlines()[0] if t.blocked_reason else ""}
                    for t in tickets if t.state == State.BLOCKED],
        "workers": workers,
        "slots": {"workers": f"{sum(1 for w in workers if w['kind'] == 'worker')}/{store.capacity()['workers']}",
                  "reviewers": f"{sum(1 for w in workers if w['kind'] == 'reviewer')}/{store.capacity()['reviewers']}"},
        "pipeline": store.pipeline(),
        "pickup_paused": store.pickup_paused(),
        "gate": gate,
        "last_gate": {"id": last["id"], "status": last["status"], "summary": last.get("summary", ""),
                      "finished_at": last.get("finished_at")} if last else None,
        "next_gate": [{"id": t.id, "title": t.title, "waiting": round(now - _ts(t.state_since))} for t in approved[:1]],
        "queued": [{"id": t.id, "title": t.title} for t in approved[1:]],
        "on_the_way": [{"id": t.id, "title": t.title, "state": t.state.value} for t in on_the_way],
        "counts": {k: v for k, v in counts.items() if v},
        "staging": staging,
    }


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return time.time()


def _ago(sec: float | None) -> str:
    if sec is None:
        return ""
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m" if sec < 3600 else f"{sec / 3600:.1f}h"


def render(d: dict) -> str:
    L: list[str] = []
    p = d["project"]
    code = p["work"] == "code"
    L.append(f"{p['key']}  {p['repo_root']}  base={p['base_branch']}  "
             + ("work=code: a branch per ticket, landed by the merge gate" if code else "work=tasks: a folder per ticket, nothing merged"))
    if d["blocked"]:
        L.append("")
        L.append(f"needs a decision: {len(d['blocked'])} blocked ticket{'s' if len(d['blocked']) != 1 else ''}")
        for b in d["blocked"]:
            L.append(f"  ■ {b['id']:10} {b['title'][:40]:40}  {b['reason'][:70]}")
    L.append("")
    sc = d["scheduler"]
    L.append(f"scheduler: {'RUNNING' if sc['running'] else 'DOWN'}" + (f"  pid {sc['pid']}" if sc["running"] else "   (start with `tasking run`)"))
    pl = d["pipeline"]
    L.append((f"pipeline:  {'PAUSED ' + str(pl.get('reason')) if pl.get('paused') else 'running'}   " if code else "")
             + f"slots: workers {d['slots']['workers']}, reviewers {d['slots']['reviewers']}" + ("   NEW WORKERS PAUSED" if d.get("pickup_paused") else ""))
    for x in d["workers"]:
        L.append(f"  ● {x['id']:24} {x['ticket']} {x['title'][:40]}  {_ago(x['elapsed'])}")
    L.append("")
    if code:
        g = d["gate"]
        if g:
            bar = "#" * (g["percent"] // 5) + "-" * (20 - g["percent"] // 5)
            L.append(f"merge gate: run {g['id']}  [{bar}] {g['percent']}%  stage {g['stages_done']}/{g['stages_total']} ({g['current_stage']})" + ("  FAILING" if g["failed_stage"] else ""))
            for t in g["tickets"]:
                L.append(f"  in gate:  {t['id']} {t['title'][:50]}")
        else:
            lg = d["last_gate"]
            L.append("merge gate: idle" + (f"  (last: {lg['status']} {lg['summary'][:60]})" if lg else ""))
        for t in d["next_gate"]:
            L.append(f"  next:     {t['id']} {t['title'][:50]}  (approved {_ago(t['waiting'])} ago)")
        for t in d["queued"]:
            L.append(f"  queued:   {t['id']} {t['title'][:50]}")
    for t in d["on_the_way"]:
        L.append(f"  in flight: {t['id']} {t['state']:17} {t['title'][:40]}")
    L.append("tickets:   " + ("  ".join(f"{k}={v}" for k, v in d["counts"].items()) or "none"))
    s = d["staging"]
    if s["configured"]:
        L.append("")
        L.append(f"staging:   {(s['status'] or 'stopped').upper()}{'' if s['alive'] else ' (no process)'}  build {s['build'] or '-'}  {s['local_url'] or ''}" +
                 (f"  {s['public_url']}" if s["public_url"] else ""))
        for line in s["log_tail"]:
            L.append(f"  | {line[:150]}")
    return "\n".join(L)
