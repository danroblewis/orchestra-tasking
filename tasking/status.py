"""One-screen status: loop, agents, web, tunnels, merge gate, queue, staging."""

from __future__ import annotations

import os
import re
import time
import urllib.request
from pathlib import Path

import psutil

from .agents.roles import ROLES, resident_roles
from .config import Project
from .model import State
from .staging import Staging
from .store import LocalStore


def _alive(pid) -> bool:
    return bool(pid) and psutil.pid_exists(int(pid))


def _http_ok(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 400
    except Exception:
        return False


def _dashboard_tunnel_hosts(port: int) -> list[str]:
    """Hostnames of *running* cloudflared named tunnels whose ingress targets this port.
    Orchestra does not manage the dashboard tunnel; this reports what is live."""
    running = " ".join(" ".join(p.info["cmdline"] or []) for p in psutil.process_iter(["name", "cmdline"])
                       if (p.info["name"] or "").startswith("cloudflared"))
    hosts: list[str] = []
    for cfg in Path("~/.cloudflared").expanduser().glob("*.y*ml"):
        try:
            text = cfg.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"^tunnel:\s*(\S+)", text, re.M)
        tunnel_id = m.group(1) if m else ""
        live = str(cfg) in running or (tunnel_id and tunnel_id in running) or (cfg.stem and f"run {cfg.stem}" in running)
        if not live:
            continue
        for host, svc in re.findall(r"hostname:\s*(\S+)\s*\n\s*service:\s*(\S+)", text):
            if svc.rstrip("/").endswith(f":{port}"):
                hosts.append(host)
    return sorted(set(hosts))


def gate_stage_plan(project: Project) -> int:
    cfg = project.config
    n = len(cfg.unit) + (1 if cfg.build_command else 0)
    if cfg.acceptance and cfg.acceptance_start:
        n += 1 + len(cfg.acceptance)
    return max(n, 1)


def collect(project: Project, store: LocalStore) -> dict:
    cfg = project.config
    rt = store.runtime()
    now = time.time()
    sessions = {s["id"]: s for s in rt.get("sessions", {}).values()}
    tickets = store.list_tickets()
    by_id = {t.id: t for t in tickets}

    ctl = rt.get("control") or {}
    loop_alive = _alive(ctl.get("pid"))
    loop_ok = loop_alive and _http_ok(f"http://127.0.0.1:{ctl.get('port', 0)}/health")

    agents = []
    for role in resident_roles():
        s = sessions.get(role.session_id, {})
        alive = _alive(s.get("pid"))
        agents.append({"id": role.session_id, "state": "working" if s.get("status") == "running" and alive
                       else "resident" if alive else "down",
                       "since": s.get("started_at"), "last": (s.get("last_text") or "")[:80]})
    workers = [{"id": s["id"], "kind": s.get("kind"), "ticket": s.get("ticket_id"),
                "title": by_id[s["ticket_id"]].title if s.get("ticket_id") in by_id else "",
                "elapsed": round(now - (s.get("started_at") or now))}
               for s in sessions.values() if s.get("kind") in ("worker", "reviewer") and s.get("status") == "running"
               and _alive(s.get("pid"))]

    web = rt.get("web") or {}
    web_alive = _alive(web.get("pid"))
    web_url = f"http://127.0.0.1:{web.get('port', 8000)}"
    web_ok = web_alive and _http_ok(web_url + "/api/state")
    dash_hosts = _dashboard_tunnel_hosts(int(web.get("port", 8000)))

    runs = store.gate_runs()
    cur = next((r for r in runs if r.get("status") == "running"), None)
    gate = None
    if cur:
        total = gate_stage_plan(project)
        done = len(cur.get("stages", []))
        failed = any(s.get("status") == "failed" for s in cur.get("stages", []))
        started = cur.get("started_at")
        gate = {"id": cur["id"], "tickets": [{"id": t, "title": by_id[t].title if t in by_id else ""} for t in cur["batch"]],
                "stages_done": done, "stages_total": total, "percent": int(100 * min(done, total) / total),
                "failed_stage": failed, "current_stage": (cur["stages"][-1]["stage"] + ":" + cur["stages"][-1]["name"]) if cur.get("stages") else "assembling",
                "started_at": started}
    last = next((r for r in reversed(runs) if r.get("status") != "running"), None)
    approved = sorted((t for t in tickets if t.state == State.APPROVED), key=lambda t: t.state_since)
    on_the_way = [t for t in tickets if t.state in (State.IN_REVIEW, State.READY_FOR_REVIEW, State.REJECTED, State.IN_PROGRESS)]

    st = Staging(project, store, store.events)
    sstate = st.state() if cfg.staging_configured else {"configured": False}
    staging = {"configured": cfg.staging_configured, "status": sstate.get("status"), "alive": sstate.get("alive"),
               "build": sstate.get("current_build_id"), "local_url": sstate.get("local_url"),
               "public_url": sstate.get("public_url"), "deployed_at": sstate.get("deployed_at"),
               "log_tail": st.logs(4000).strip().splitlines()[-6:] if cfg.staging_configured else []}

    counts = {s.value: sum(1 for t in tickets if t.state == s) for s in State}
    return {
        "project": {"key": project.key, "repo_root": str(project.repo_root), "base_branch": cfg.base_branch,
                    "model": f"{cfg.provider or 'default'}/{cfg.model or 'default'}"},
        "loop": {"running": loop_ok, "pid": ctl.get("pid"), "control_port": ctl.get("port")},
        "agents": agents,
        "workers": workers,
        "slots": {"workers": f"{sum(1 for w in workers if w['kind'] == 'worker')}/{store.capacity()['workers']}",
                  "reviewers": f"{sum(1 for w in workers if w['kind'] == 'reviewer')}/{store.capacity()['reviewers']}"},
        "web": {"up": web_ok, "url": web_url if web_alive else None, "pid": web.get("pid"), "tunnel_hosts": dash_hosts},
        "pipeline": rt.get("pipeline", {}),
        "pickup_paused": bool(rt.get("pickup_paused")),
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
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return time.time()


def _ago(sec: float | None) -> str:
    if sec is None:
        return ""
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m" if sec < 3600 else f"{sec / 3600:.1f}h"


def render(d: dict) -> str:
    L: list[str] = []
    p = d["project"]
    L.append(f"{p['key']}  {p['repo_root']}  base={p['base_branch']}  model={p['model']}")
    lp = d["loop"]
    L.append(f"loop:      {'RUNNING' if lp['running'] else 'DOWN'}" + (f"  pid {lp['pid']}  control 127.0.0.1:{lp['control_port']}" if lp["running"] else "   (start with `tasking run`)"))
    for a in d["agents"]:
        mark = {"working": "●", "resident": "○", "down": "✗"}[a["state"]]
        L.append(f"  {mark} {a['id']:17} {a['state']:9} {a['last']}")
    w = d["web"]
    L.append(f"web:       {'UP' if w['up'] else 'DOWN'}" + (f"  {w['url']}" if w["url"] else "   (start with `orchestra web`)") +
             (f"  tunnel: " + ", ".join("https://" + h for h in w["tunnel_hosts"]) if w["tunnel_hosts"] else ""))
    pl = d["pipeline"]
    L.append(f"pipeline:  {'PAUSED ' + str(pl.get('reason')) if pl.get('paused') else 'running'}   slots: workers {d['slots']['workers']}, reviewers {d['slots']['reviewers']}" + ("   NEW WORKERS PAUSED" if d.get("pickup_paused") else ""))
    for x in d["workers"]:
        L.append(f"  ● {x['id']:24} {x['ticket']} {x['title'][:40]}  {_ago(x['elapsed'])}")
    L.append("")
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
        L.append(f"  upcoming: {t['id']} {t['state']:17} {t['title'][:40]}")
    L.append("tickets:   " + ("  ".join(f"{k}={v}" for k, v in d["counts"].items()) or "none"))
    L.append("")
    s = d["staging"]
    if not s["configured"]:
        L.append("staging:   not configured")
    else:
        L.append(f"staging:   {(s['status'] or 'stopped').upper()}{'' if s['alive'] else ' (no process)'}  build {s['build'] or '-'}  {s['local_url'] or ''}" +
                 (f"  {s['public_url']}" if s["public_url"] else ""))
        for line in s["log_tail"]:
            L.append(f"  | {line[:150]}")
    return "\n".join(L)
