"""The `tasking` command line: the task system's loop, its tickets, the agents' tool interface.

Lifted from Orchestra's cli.py as it was before the task system was removed (5006caf^); the
web UI, metrics, model and event commands stay Orchestra's (`orchestra web`, …)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import agents, api
from .agents.roles import ROLES
from .config import ConfigError, Project, init_project, load_project
from .events import Events
from .model import STATE_ORDER, State
from .store import LocalStore, StoreError


def _project(args) -> Project:
    try:
        return load_project(getattr(args, "repo", None))
    except ConfigError as e:
        sys.exit(f"tasking: {e}")


def _store(project: Project) -> tuple[LocalStore, Events]:
    events = Events.for_project(project)
    return LocalStore(project, events), events


# -- commands --------------------------------------------------------------------

def cmd_init(args) -> int:
    p = init_project(Path(args.repo or os.getcwd()), provider=args.provider, model=args.model,
                     prefix=args.prefix, unit_command=args.unit_command)
    print(f"initialized {p.key}\n  repo:   {p.repo_root}\n  state:  {p.state_dir}\n  config: {p.repo_root / 'orchestra.toml'}")
    return 0


def cmd_run(args) -> int:
    from .loop import Loop

    project = _project(args)
    store, events = _store(project)
    Loop(project, store, events).run(once=args.once)
    return 0


def cmd_status(args) -> int:
    from . import status as st

    project = _project(args)
    store, _ = _store(project)
    d = st.collect(project, store)
    print(json.dumps(d, indent=1) if args.json else st.render(d))
    return 0


def cmd_ticket(args) -> int:
    project = _project(args)
    store, _ = _store(project)
    if args.action == "list":
        for t in store.list_tickets():
            print(f"{t.id:10} {t.state.value:17} p{t.priority} {t.milestone_id:4} {t.title}")
        return 0
    t = store.get_ticket(args.id)
    print(json.dumps({**t.to_dict(), "notes": store.notes(t.id)}, indent=2))
    return 0


def cmd_milestone(args) -> int:
    project = _project(args)
    store, _ = _store(project)
    if args.action == "list":
        for m in store.list_milestones():
            n = len(store.list_tickets(milestone_id=m.id))
            print(f"{m.id:5} {m.status.value:7} {n:3} tickets  {m.name}")
        return 0
    print(json.dumps(store.get_milestone(args.id).to_dict(), indent=2))
    return 0


def cmd_sessions(args) -> int:
    from .harness import read_transcript

    project = _project(args)
    store, _ = _store(project)
    if args.action == "list":
        for s in store.sessions():
            print(f"{s['id']:28} {s.get('status', '?'):8} {s.get('ticket_id') or '':10} {(s.get('last_text') or '')[:60]!r}")
        return 0
    s = next((s for s in store.sessions() if s["id"] == args.id), None)
    if not s or not s.get("session_file"):
        sys.exit(f"no transcript for {args.id}")
    for m in read_transcript(s):
        tools = " ".join(f"[{t['name']}]" for t in m["tool_calls"])
        print(f"--- {m['role']} {tools}\n{m['text'][:3000]}\n")
    return 0


def cmd_tools(args) -> int:
    from .harness.tools import TOOLS, tools_for

    groups = args.groups or os.environ.get("ORCHESTRA_TOOLS")
    tools = tools_for([g for g in groups.split(",") if g]) if groups else list(TOOLS)
    if args.json:
        print(json.dumps([t.to_dict() for t in tools]))
        return 0
    for t in tools:
        print(f"{t.name:26} {t.group:14} {t.description[:80]}")
    return 0


def cmd_api(args) -> int:
    project = _project(args)
    store, _ = _store(project)
    try:
        payload = json.loads(args.args) if args.args else {}
        out = api.handle(project, store, args.command, payload,
                         role=os.environ.get("ORCHESTRA_ROLE", ""), ticket_env=os.environ.get("ORCHESTRA_TICKET", ""))
    except (StoreError, KeyError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps(out, indent=None if args.compact else 1))
    return 0


def _print_event(ev: dict) -> None:
    t = ev.get("type")
    if t == "tool_start":
        print(f"  ⚙ {ev.get('name', 'tool')} {json.dumps(ev.get('args') or {})[:160]}", flush=True)
    elif t == "assistant_end":
        content = ev.get("text") or ""
        if isinstance(content, list):
            txt = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            if txt.strip():
                print(f"\n{txt.strip()}\n", flush=True)


def _chat_attached(project: Project, store: LocalStore, port: int) -> int:
    import urllib.request

    print(f"Supervisor for {project.key} via tasking run on 127.0.0.1:{port}. Ctrl-D or 'exit' to leave.\n")
    while True:
        try:
            line = input("you> ").strip()
        except EOFError:
            break
        if line in ("exit", "quit"):
            break
        if not line:
            continue
        req = urllib.request.Request(f"http://127.0.0.1:{port}/supervisor", method="POST",
                                     data=json.dumps({"message": line}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=(project.config.turn_timeout or 86400) + 30) as resp:
                for raw in resp:
                    ev = json.loads(raw)
                    if ev.get("type") == "done":
                        if ev.get("error") or ev.get("timed_out"):
                            print(f"[tasking] turn ended: {ev.get('error') or 'timed out'}")
                    else:
                        _print_event(ev)
        except Exception as e:
            print(f"[tasking] lost the loop: {e}. Is `tasking run` still up?")
            return 1
    return 0


def cmd_chat(args) -> int:
    from .harness import SessionSpec, get_harness

    project = _project(args)
    store, events = _store(project)
    cfg = project.config
    ctl = store.runtime().get("control") or {}
    if ctl.get("port"):
        import urllib.request
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{ctl['port']}/health", timeout=3)
            return _chat_attached(project, store, int(ctl["port"]))
        except Exception:
            pass
    print("[tasking] `tasking run` is not running; starting a temporary supervisor session for this chat.")
    sid = "supervisor"
    role = ROLES["supervisor"]
    proc = get_harness(cfg.harness).start(SessionSpec(
        session_id=sid, session_dir=project.sessions_dir, cwd=project.repo_root,
        system_prompt=agents.role_prompt(project, "supervisor"), role="supervisor", tool_groups=tuple(role.tools),
        env={"ORCHESTRA_ROLE": "supervisor", "ORCHESTRA_REPO": str(project.repo_root), "ORCHESTRA_TICKET": ""},
        log_path=project.logs_dir / "agents" / "supervisor.stderr.log",
        model=cfg.model, provider=cfg.provider, thinking=cfg.thinking))
    try:
        proc.open()
    except Exception:
        pass
    store.set_session(sid, kind="supervisor", status="idle", pid=proc.pid or os.getpid(),
                      session_file=proc.session_file, harness=proc.harness_name)
    _, offset = events.read(0, limit=1)
    print(f"Supervisor for {project.key} ({cfg.harness}: {cfg.provider or ''}/{cfg.model or 'default'}). Ctrl-D or 'exit' to leave.\n")
    try:
        while True:
            try:
                line = input("you> ").strip()
            except EOFError:
                break
            if line in ("exit", "quit"):
                break
            if not line:
                continue
            new, offset = events.read(offset)
            new = [e for e in new if e["type"] not in ("session_started", "session_settled")]
            prefix = agents.supervisor_context(store.snapshot(), new, project, store)
            store.set_session(sid, status="running", started_at=time.time())
            events.emit("session_started", session=sid, kind="supervisor")
            res = proc.prompt(prefix + line, cfg.turn_timeout or None, on_event=_print_event, idle_timeout=cfg.silence_timeout)
            store.set_session(sid, status="idle", last_text=(res.text or "")[-500:], settled_at=time.time())
            events.emit("session_settled", session=sid)
            if res.error or res.timed_out:
                print(f"[tasking] turn ended: {res.error or 'timed out'}")
    finally:
        proc.close()
        store.set_session(sid, status="idle", pid=None)
    return 0


def cmd_staging(args) -> int:
    from .gate.runner import run_specs
    from .git import Git
    from .staging import Staging, record_build

    project = _project(args)
    store, events = _store(project)
    st = Staging(project, store, events)
    if not project.config.staging_configured:
        sys.exit("tasking: no [staging] section in orchestra.toml")
    if args.action == "status":
        print(json.dumps({k: v for k, v in st.state().items()}, indent=2))
    elif args.action == "logs":
        print(st.logs())
    elif args.action == "stop":
        st.stop(); print("stopped")
    elif args.action == "restart":
        r = st.restart()
        print({"ok": "restarted", "no_build": "no build recorded; use `tasking staging deploy`",
               "start_failed": "start failed; see `tasking staging logs`",
               "tunnel_failed": "running locally, but the tunnel probe failed; see staging/tunnel.log"}[r])
        if r in ("ok", "tunnel_failed"):
            print(f"local:  {st.local_url()}" + (f"\npublic: {st.public_url()}" if st.public_url() else ""))
    elif args.action == "deploy":
        # Build the current base branch head with the [build] stage and promote it.
        import glob
        import shutil
        import uuid

        cfg = project.config
        if not (cfg.build_command and cfg.build_artifact):
            sys.exit("tasking: [build] command and artifact are required to deploy")
        git = Git(project.repo_root)
        commit = git.rev_parse(cfg.base_branch)
        bid = f"manual-{uuid.uuid4().hex[:8]}"
        path = project.gate_dir / bid
        git.worktree_add(f"orchestra/build-{bid}", path, cfg.base_branch)
        try:
            from .config import TestSpec
            ok, res = run_specs([TestSpec("build", cfg.build_command, timeout=cfg.build_timeout)], path,
                                project.logs_dir / bid, stage="build")
            if not ok:
                sys.exit(f"tasking: build failed:\n{res[-1]['tail']}")
            matches = sorted(glob.glob(str(path / cfg.build_artifact)))
            if not matches:
                sys.exit(f"tasking: no artifact matched {cfg.build_artifact}")
            dest = project.artifacts_dir / bid
            dest.mkdir(parents=True, exist_ok=True)
            artifact = shutil.copy(matches[-1], dest)
        finally:
            git.worktree_remove(path)
            git.branch_delete(f"orchestra/build-{bid}")
        build = {"id": bid, "gate_run_id": None, "commit": commit, "artifact_path": str(artifact),
                 "created_at": __import__("tasking.model", fromlist=["now"]).now()}
        record_build(store, build)
        print(f"built {Path(artifact).name} from {commit[:10]}; deploying")
        print("deployed" if st.deploy(build) else "deploy FAILED; see `tasking staging logs`")
        print(f"local:  {st.local_url()}" + (f"\npublic: {st.public_url()}" if st.public_url() else ""))
    return 0


def cmd_roles(args) -> int:
    from . import api as api_mod

    project = _project(args)
    store, _ = _store(project)
    if args.action == "list":
        for r in api_mod.handle(project, store, "list_roles", {})["roles"]:
            kind = "built-in" if r["builtin"] else "custom"
            wake = ", ".join(r.get("wake_on") or []) + (f" every {r['every']}" if r.get("every") else "") + (f" at {r['at']}" if r.get("at") else "")
            print(f"{r['name']:20} {kind:9} {r['session']:9} {'paused' if r['paused'] else '':7} {wake.strip() or '-'}")
        return 0
    print(json.dumps(api_mod.handle(project, store, "get_role", {"name": args.name}), indent=2))
    return 0


def cmd_doctor(args) -> int:
    from .git import Git

    project = _project(args)
    store, _ = _store(project)
    git = Git(project.repo_root)
    known = {w["path"] for w in store.runtime()["worktrees"].values()}
    problems = 0
    for p in project.worktrees_dir.glob("*"):
        if str(p) not in known:
            problems += 1
            print(f"orphan worktree: {p}")
            if args.fix:
                git.worktree_remove(p)
    if not project.config.unit:
        print("warning: no [[gate.unit]] specs; the gate and reviews run no tests")
    if not project.config.build_command or not project.config.acceptance:
        print("warning: no [build]/[[gate.acceptance]]; the gate cannot prove the built artifact works")
    print("ok" if not problems else f"{problems} problem(s)")
    return 0


# -- parser ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tasking", description="The task system: tickets, the loop that assigns them to agents, the merge gate")
    p.add_argument("--repo", help="repository root (default: ORCHESTRA_REPO or cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="register this repository and write orchestra.toml")
    s.add_argument("--provider", help="Pi provider (default: Pi's own default)")
    s.add_argument("--model", help="Pi model id (default: Pi's own default)")
    s.add_argument("--prefix", help="ticket id prefix, e.g. ORC")
    s.add_argument("--unit-command", default="uv run pytest -q")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("run", help="run the orchestration loop and the long-lived agent sessions")
    s.add_argument("--once", action="store_true", help="one tick, wait for its work, exit")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("chat", help="talk to the Supervisor")
    s.set_defaults(fn=cmd_chat)

    s = sub.add_parser("status", help="loop, agents, web, tunnels, merge gate, queue, staging")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("ticket")
    s.add_argument("action", choices=["list", "show"])
    s.add_argument("id", nargs="?")
    s.set_defaults(fn=cmd_ticket)

    s = sub.add_parser("milestone")
    s.add_argument("action", choices=["list", "show"])
    s.add_argument("id", nargs="?")
    s.set_defaults(fn=cmd_milestone)

    s = sub.add_parser("sessions")
    s.add_argument("action", choices=["list", "show"])
    s.add_argument("id", nargs="?")
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("tools", help="the Orchestra tools agents get, as data (used by the Pi extension)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--groups", help="comma-separated tool groups (default: ORCHESTRA_TOOLS, else all)")
    s.set_defaults(fn=cmd_tools)

    s = sub.add_parser("api", help="machine interface the agent tools call")
    s.add_argument("command")
    s.add_argument("--args", default="{}", help="JSON object of arguments")
    s.add_argument("--compact", action="store_true")
    s.set_defaults(fn=cmd_api)

    s = sub.add_parser("staging", help="staging environment: status, logs, deploy, restart, stop")
    s.add_argument("action", choices=["status", "logs", "deploy", "restart", "stop"])
    s.set_defaults(fn=cmd_staging)

    s = sub.add_parser("roles", help="agent roles: built-in and custom")
    s.add_argument("action", choices=["list", "show"])
    s.add_argument("name", nargs="?")
    s.set_defaults(fn=cmd_roles)

    s = sub.add_parser("doctor")
    s.add_argument("--fix", action="store_true")
    s.set_defaults(fn=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
