"""`orchestra tasks`: the task system's command line (its scheduler, its tickets, its operations),
a subcommand of Orchestra's (entry point group ``orchestra.commands``; Orchestra sets
``ORCHESTRA_PROG``). ``python -m tasking`` runs the same. The web UI, metrics, model and event
commands are Orchestra's (`orchestra web`, …). What the command line publishes (a staging deploy,
a planner's request) is sourced ``task_cli``, or the role a request is sent as."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from .config import ConfigError, Project, init_project, load_project
from .events import SCHEDULER, Events
from .requests import QUERIES, REQUESTS, ask
from .store import LocalStore, StoreError

CLI = "task_cli"


def prog() -> str:
    """How the command is called: ``ORCHESTRA_PROG`` (Orchestra sets it), else ``orchestra tasks``."""
    return os.environ.get("ORCHESTRA_PROG") or "orchestra tasks"


def _project(args) -> Project:
    try:
        return load_project(getattr(args, "repo", None))
    except ConfigError as e:
        sys.exit(f"{prog()}: {e}")


def _store(project: Project, source: str = CLI) -> tuple[LocalStore, Events]:
    events = Events.for_project(project, source=source)
    return LocalStore(project, events), events


def cmd_init(args) -> int:
    p = init_project(Path(args.repo or os.getcwd()), prefix=args.prefix, unit_command=args.unit_command, base_branch=args.base_branch)
    print(f"initialized {p.key}\n  repo:   {p.repo_root}\n  state:  {p.state_dir}\n  config: {p.orchestra.config_file}"
          f"\n  base:   {p.config.base_branch} (approved work lands here; [project] base_branch)")
    return 0


def cmd_run(args) -> int:
    from .scheduler import Scheduler

    project = _project(args)
    store, events = _store(project, source=SCHEDULER)
    Scheduler(project, store, events).run(once=args.once, wait=args.wait)
    return 0


def cmd_metrics(args) -> int:
    """The task system's measurements as Prometheus text, from the record (tasking/measures.py):
    the instrument the plugin declares runs this (tasking/web.py)."""
    project = _project(args)
    store, _ = _store(project)
    sys.stdout.write(store.metrics_text())
    return 0


def cmd_status(args) -> int:
    from . import status as st

    project = _project(args)
    store, events = _store(project)
    d = st.collect(project, store, events)
    print(json.dumps(d, indent=1) if args.json else st.render(d))
    return 0


def cmd_ticket(args) -> int:
    project = _project(args)
    store, _ = _store(project)
    if args.action == "list":
        for t in store.list_tickets():
            print(f"{t.id:10} {t.state.value:17} p{t.priority} {t.milestone_id:4} {t.title}")
        return 0
    if not args.id:
        sys.exit(f"{prog()}: ticket show needs an id")
    try:
        t = store.get_ticket(args.id)
    except StoreError:
        sys.exit(f"{prog()}: no ticket {args.id}")
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
    if not args.id:
        sys.exit(f"{prog()}: milestone show needs an id")
    try:
        print(json.dumps(store.get_milestone(args.id).to_dict(), indent=2))
    except StoreError:
        sys.exit(f"{prog()}: no milestone {args.id}")
    return 0


def cmd_api(args) -> int:
    """One request to the task system over the bus, as the planner ``--as`` names, answered by
    the running scheduler's desk (tasking/requests.py). With no operation, the operations."""
    if not args.command:
        print(f"changes: {', '.join(REQUESTS)}\nqueries: {', '.join(QUERIES)}")
        return 0
    project = _project(args)
    try:
        payload = json.loads(args.args) if args.args else {}
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"--args is not JSON: {e}"}))
        return 1
    out = ask(Events.for_project(project, source=args.by), args.command, payload, args.by)
    print(json.dumps(out, indent=None if args.compact else 1))
    return 0 if out.get("ok", True) else 1


def cmd_staging(args) -> int:
    from .gate import build
    from .git import Git
    from .model import now
    from .staging import Staging

    project = _project(args)
    store, events = _store(project)
    st = Staging(project, store, events)
    if not project.config.staging_configured:
        sys.exit(f"{prog()}: no [staging] section in orchestra.toml")
    if args.action == "status":
        print(json.dumps(st.state(), indent=2))
    elif args.action == "logs":
        print(st.logs())
    elif args.action == "stop":
        st.stop()
        print("stopped")
    elif args.action == "restart":
        r = st.restart()
        print({"ok": "restarted", "no_build": "no build recorded; use `orchestra tasks staging deploy`",
               "start_failed": "start failed; see `orchestra tasks staging logs`",
               "tunnel_failed": "running locally, but the tunnel probe failed; see staging/tunnel.log"}[r])
        if r in ("ok", "tunnel_failed"):
            print(f"local:  {st.local_url()}" + (f"\npublic: {st.public_url()}" if st.public_url() else ""))
    elif args.action == "deploy":
        # build the current base branch head with the [build] stage (the gate's) and promote it
        cfg = project.config
        if not (cfg.build_command and cfg.build_artifact):
            sys.exit(f"{prog()}: [build] command and artifact are required to deploy")
        git = Git(project.repo_root)
        commit = git.rev_parse(cfg.base_branch)
        bid = f"manual-{uuid.uuid4().hex[:8]}"
        path = project.gate_dir / bid
        git.worktree_add(f"orchestra/build-{bid}", path, cfg.base_branch)
        try:
            res, artifact = build(project, path, project.logs_dir / bid, bid)
        finally:
            git.worktree_remove(path)
            git.branch_delete(f"orchestra/build-{bid}")
        if artifact is None:
            sys.exit(f"{prog()}: build failed:\n{res[-1]['tail']}")
        rec = {"id": bid, "gate_run_id": None, "commit": commit, "artifact_path": artifact, "created_at": now()}
        store.record_build(rec)
        print(f"built {Path(artifact).name} from {commit[:10]}; deploying")
        print("deployed" if st.deploy(rec) else "deploy FAILED; see `orchestra tasks staging logs`")
        print(f"local:  {st.local_url()}" + (f"\npublic: {st.public_url()}" if st.public_url() else ""))
    return 0


def cmd_stack(args) -> int:
    """The tickets' branches with the base each one's pull request is to have (tasking/stacks.py):
    what the owner needs to submit a stack. Read only."""
    from .stacks import listing

    project = _project(args)
    store, _ = _store(project)
    rows = listing(project, store)
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    for r in rows:
        print(f"{r['ticket']:10} {r['state']:18} {r['branch']}  <- {r['base']}" + (f" ({r['parent']})" if r["parent"] else "")
              + ("" if r["on_base"] else "  [needs restack]") + (f"  {r['pull']}" if r["pull"] else ""))
    if not rows:
        print("no branches")


def cmd_recurring(args) -> int:
    """``check``: the [[tasks.recurring]] definitions, and what is wrong with each (exit 1 when
    anything is); ``list``: each with its schedule, next due time and last occurrence."""
    from datetime import datetime

    from . import recurring

    project = _project(args)
    store, _ = _store(project)
    v = recurring.view(project, store)
    if args.json:
        print(json.dumps(v, indent=1))
        return 0 if v["ok"] or args.action == "list" else 1
    for r in v["recurring"]:
        last = r["last"] or {}
        nxt = r["next_local"] or "disabled"
        print(f"{r['name']:24} {r['schedule']:44} next {nxt}" + (f"  last {last.get('occurrence')}: {last.get('outcome')}"
                                                                  + (f" {last['ticket']}" if last.get("ticket") else "") if last else ""))
    for label, why in v["problems"].items():
        print(f"{label}: not used:\n" + "\n".join(f"  - {w}" for w in why))
    if args.action == "check":
        n = len(v["recurring"])
        print("ok" if v["ok"] else f"{len(v['problems'])} definition(s) refused", f"({n} valid, as of {datetime.now().astimezone():%Y-%m-%d %H:%M %Z})")
        return 0 if v["ok"] else 1
    if not v["recurring"] and not v["problems"]:
        print("no recurring tickets ([[tasks.recurring]] in orchestra.toml)")
    return 0


def cmd_doctor(args) -> int:
    from .git import Git

    project = _project(args)
    store, _ = _store(project)
    git = Git(project.repo_root)
    known = {w["path"] for w in store.worktrees().values()}
    problems = 0
    for p in project.worktrees_dir.glob("*"):
        if str(p) not in known:
            problems += 1
            print(f"orphan worktree: {p}")
            if args.fix:
                git.worktree_remove(p)
    slots = {s["path"] for s in store.pool().values()}
    for p in project.pool_dir.glob("*"):
        if str(p) not in slots:
            problems += 1
            print(f"orphan pool worktree: {p}")
            if args.fix:
                git.worktree_remove(p)
    if len(slots) > project.config.worktree_pool:
        print(f"note: the pool holds {len(slots)} worktrees, more than [tasks] worktree_pool = {project.config.worktree_pool}; "
              "none is made beyond it, and the extra ones stay until removed by hand")
    if project.config.work == "code":
        if not project.config.unit:
            print("warning: no [[gate.unit]] specs; the gate and reviews run no tests")
        if not project.config.build_command or not project.config.acceptance:
            print("warning: no [build]/[[gate.acceptance]]; the gate cannot prove the built artifact works")
    print("ok" if not problems else f"{problems} problem(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog(), description="The task system: tickets, the scheduler that offers them to workers and reviewers, the merge gate")
    p.add_argument("--repo", help="repository root (default: ORCHESTRA_REPO or cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="register this repository and write orchestra.toml")
    s.add_argument("--prefix", help="ticket id prefix, e.g. ORC")
    s.add_argument("--unit-command", default="uv run pytest -q", help="the [[gate.unit]] command to start with")
    s.add_argument("--base-branch", help="where approved work lands (default: the remote's default branch, else the one checked out)")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("run", help="run the scheduler: offer ready tickets to the roles, apply their reports, land approved code")
    s.add_argument("--once", action="store_true", help="one tick, wait for its work, exit")
    s.add_argument("--wait", action="store_true", help="when another scheduler runs this project, wait for it to stop instead of exiting "
                                                       "(how orchestra run starts it)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("metrics", help="the measurements (ticket levels and transition counters) as Prometheus text, for the instrument")
    s.set_defaults(fn=cmd_metrics)

    s = sub.add_parser("status", help="one screen: what needs a decision, the scheduler, who holds what, the queue, the gate, staging")
    s.add_argument("--json", action="store_true", help="the same as JSON")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("ticket", help="read the tickets")
    s.add_argument("action", choices=["list", "show"], help="list every ticket, or show one with its notes")
    s.add_argument("id", nargs="?", help="the ticket to show, e.g. ORC-1")
    s.set_defaults(fn=cmd_ticket)

    s = sub.add_parser("milestone", help="read the milestones")
    s.add_argument("action", choices=["list", "show"], help="list every milestone, or show one")
    s.add_argument("id", nargs="?", help="the milestone to show, e.g. M1")
    s.set_defaults(fn=cmd_milestone)

    s = sub.add_parser("api", help="ask the running scheduler for one operation (create_ticket, list_tickets, …) as a planner; JSON out")
    s.add_argument("command", nargs="?", help=f"the operation. changes: {', '.join(REQUESTS)}; queries: {', '.join(QUERIES)}")
    s.add_argument("--args", default="{}", help="JSON object of arguments")
    s.add_argument("--as", dest="by", default="planner", help="the role asking (the event's source and its `by`); default planner")
    s.add_argument("--compact", action="store_true", help="JSON on one line")
    s.set_defaults(fn=cmd_api)

    s = sub.add_parser("staging", help="staging environment: status, logs, deploy, restart, stop")
    s.add_argument("action", choices=["status", "logs", "deploy", "restart", "stop"])
    s.set_defaults(fn=cmd_staging)

    s = sub.add_parser("stack", help="code mode: each ticket's branch and the branch its pull request is based on (stacked branches)")
    s.add_argument("--json", action="store_true", help="the same as JSON")
    s.set_defaults(fn=cmd_stack)
    s = sub.add_parser("recurring", help="recurring tickets: check the [[tasks.recurring]] definitions, or list them with their next due times")
    s.add_argument("action", choices=["check", "list"], help="check: what is wrong with each definition (exit 1 if any); list: next due times")
    s.add_argument("--json", action="store_true", help="the same as JSON")
    s.set_defaults(fn=cmd_recurring)

    s = sub.add_parser("doctor", help="orphan worktrees (a ticket's or the pool's) and, in code mode, missing gate specs")
    s.add_argument("--fix", action="store_true", help="remove the orphan worktrees")
    s.set_defaults(fn=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    """``orchestra tasks ARGS``: Orchestra calls this with the arguments after ``tasks``."""
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return args.fn(args)


main.help = "the task system: tickets, the scheduler, the merge gate (init, status, api, ...)"

if __name__ == "__main__":
    sys.exit(main())
