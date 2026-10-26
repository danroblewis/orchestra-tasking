"""The `tasking` command line: the task system's scheduler, its tickets, and its operations. The
web UI, metrics, model and event commands are Orchestra's (`orchestra web`, …)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import ConfigError, Project, init_project, load_project
from .events import Events
from .requests import ask
from .store import LocalStore


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
    p = init_project(Path(args.repo or os.getcwd()), prefix=args.prefix, unit_command=args.unit_command)
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


def cmd_api(args) -> int:
    """One request to the task system over the bus, as the planner ``--as`` names, answered by
    the running scheduler's desk (tasking/requests.py)."""
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
    p = argparse.ArgumentParser(prog="tasking", description="The task system: tickets, the scheduler that offers them to workers and reviewers, the merge gate")
    p.add_argument("--repo", help="repository root (default: ORCHESTRA_REPO or cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="register this repository and write orchestra.toml")
    s.add_argument("--prefix", help="ticket id prefix, e.g. ORC")
    s.add_argument("--unit-command", default="uv run pytest -q")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("run", help="run the scheduler: offer ready tickets to the roles, apply their reports, land approved code")
    s.add_argument("--once", action="store_true", help="one tick, wait for its work, exit")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("status", help="scheduler, assignments, merge gate, queue, staging")
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

    s = sub.add_parser("api", help="ask the running scheduler for one operation (create_ticket, list_tickets, …) as a planner; JSON out")
    s.add_argument("command", help="the operation (tasking/requests.py: REQUESTS and QUERIES)")
    s.add_argument("--args", default="{}", help="JSON object of arguments")
    s.add_argument("--as", dest="by", default="planner", help="the role asking (the event's source and its `by`); default planner")
    s.add_argument("--compact", action="store_true")
    s.set_defaults(fn=cmd_api)

    s = sub.add_parser("staging", help="staging environment: status, logs, deploy, restart, stop")
    s.add_argument("action", choices=["status", "logs", "deploy", "restart", "stop"])
    s.set_defaults(fn=cmd_staging)

    s = sub.add_parser("doctor")
    s.add_argument("--fix", action="store_true")
    s.set_defaults(fn=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
