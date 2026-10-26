"""The task system's demo, in one command, from this repository's environment:

    uv run demo/run.py [target] [--port 8071]       # target: a new directory, default ~/orchestra-tasking-demo

It copies the template beside it (orchestra.toml, README.md, machines/) to the target, makes it
a git repository (Orchestra wants orchestra.toml at a repository root), removes any state a
previous run left for it, starts Orchestra's host and web UI, the task system's scheduler and the
two machines, creates a milestone and four tickets as a planner would (requests over the bus,
tasking/requests.py), prints the URL and stops everything on Ctrl-C. The processes' output goes
to run/*.log in the target. The e2e tests use the same functions against a temporary directory
and a free port (tests/e2e/test_demo_e2e.py)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from orchestra.config import home, project_key

HERE = Path(__file__).resolve().parent
PYTHON = sys.executable
PLAN = {"milestone": {"name": "Demo", "description": "Four tasks done by programs: two succeed, two need a decision."},
        "tickets": [
            {"title": "Say hello", "description": "Write a greeting into a file and show it.\n\n```sh\nsleep 4\necho 'hello from the task system' > hello.txt\ncat hello.txt\n```",
             "acceptance": "- `hello.txt` exists\n- RESULT.md shows \"hello from the task system\""},
            {"title": "Report the Python version", "depends_on": [0], "description": "```sh\npython3 --version\n```",
             "acceptance": "- RESULT.md shows \"Python 3\""},
            {"title": "Decide the project's name", "description": "Pick a name for the project and write it to NAME.txt. (No commands: the worker cannot do this.)",
             "acceptance": "- `NAME.txt` exists\n- The name is short and pronounceable."},
            {"title": "Write today's date", "description": "```sh\ndate > today.txt\ncat today.txt\n```",
             "acceptance": "- `today.txt` exists\n- `yesterday.txt` exists (nothing writes it: the reviewer rejects, and the worker is blocked)"},
        ]}


def make_project(target: Path) -> Path:
    """The template as a fresh git repository at ``target`` (which must not exist yet), with no
    state: Orchestra names a project's state directory after the target's path, so a previous
    run of the demo at the same path left tickets and events there that would show up again."""
    if target.exists():
        sys.exit(f"{target} exists; remove it or name another directory")
    shutil.copytree(HERE, target, ignore=shutil.ignore_patterns("run.py", "__pycache__"))
    git = ["git", "-c", "user.name=demo", "-c", "user.email=demo@example.invalid"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=target, check=True)
    subprocess.run([*git, "add", "-A"], cwd=target, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "the task system's demo"], cwd=target, check=True)
    state = home() / project_key(target)
    if state.exists():
        shutil.rmtree(state)
        print(f"removed the state a previous run left in {state}", flush=True)
    return target


def start(target: Path, port: int, env: dict[str, str] | None = None) -> dict[str, subprocess.Popen]:
    """Orchestra's host and web UI, the scheduler and the two machines, each with its log in run/."""
    env = {**os.environ, **(env or {}), "ORCHESTRA_REPO": str(target), "PYTHONUNBUFFERED": "1"}
    (target / "run").mkdir(exist_ok=True)
    commands = {"host": [PYTHON, "-m", "orchestra", "run"], "web": [PYTHON, "-m", "orchestra", "web", "--port", str(port)],
                "scheduler": [PYTHON, "-m", "tasking", "run"],
                "worker": [PYTHON, str(target / "machines" / "shell_worker.py")],
                "reviewer": [PYTHON, str(target / "machines" / "check_reviewer.py")]}
    procs = {}
    for name, cmd in commands.items():
        with (target / "run" / f"{name}.log").open("ab") as log:
            procs[name] = subprocess.Popen(cmd, cwd=target, env=env, stdout=log, stderr=subprocess.STDOUT)
    return procs


def stop(procs: dict[str, subprocess.Popen]) -> None:
    for p in procs.values():
        if p.poll() is None:
            p.terminate()
    for p in procs.values():
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)


def wait_until(what: str, check, timeout_s: float = 60.0) -> None:
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            up = check()
        except Exception:  # noqa: BLE001 - not up yet
            up = False
        if up:
            return
        time.sleep(0.5)
    raise TimeoutError(f"{what} did not come up within {timeout_s:.0f} s")


def wait_for(url: str, procs: dict[str, subprocess.Popen]) -> None:
    """The web UI answers and the scheduler is up (its desk then hears requests)."""
    def alive():
        dead = [n for n, p in procs.items() if p.poll() is not None]
        if dead:
            raise SystemExit(f"{', '.join(dead)} exited; see run/*.log")
        return True
    wait_until("the web UI", lambda: alive() and get(f"{url}/api/plugins")["ok"])
    wait_until("the scheduler", lambda: alive() and get(f"{url}/api/plugins/tasks/board")["scheduler"])


def plan(target: Path) -> list[str]:
    """A planner's requests: the milestone and the tickets. Returns the ticket ids."""
    from tasking.config import load_project
    from tasking.events import Events
    from tasking.requests import ask

    events = Events.for_project(load_project(target), source="planner")

    def request(command: str, args: dict) -> dict:
        out = ask(events, command, args, "planner")
        if not out.get("ok"):
            sys.exit(f"{command} refused: {out.get('error')}")
        return out

    milestone = request("create_milestone", PLAN["milestone"])["milestone"]["id"]
    ids: list[str] = []
    for t in PLAN["tickets"]:
        args = {**t, "milestone_id": milestone, "depends_on": [ids[i] for i in t.get("depends_on", [])]}
        ids.append(request("create_ticket", args)["ticket"]["id"])
    return ids


def interrupt(*_) -> None:
    """A signal stops everything, once: a second one while stopping (a `kill` of the uv chain
    sends two) changes nothing."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise KeyboardInterrupt


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target", nargs="?", default=Path.home() / "orchestra-tasking-demo", type=Path)
    ap.add_argument("--port", type=int, default=8071)
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, interrupt)          # `kill` stops everything, like Ctrl-C
    signal.signal(signal.SIGINT, interrupt)
    target, url = make_project(a.target.expanduser().resolve()), f"http://127.0.0.1:{a.port}"
    procs = start(target, a.port)
    try:
        wait_for(url, procs)
        ids = plan(target)
        print(f"\nThe demo runs at {url}\n  project:  {target}\n  tickets:  {', '.join(ids)}  ({url}/tickets)\n"
              f"  machines: {url}/machines\n  logs:     {target / 'run'}/*.log\nCtrl-C stops everything.", flush=True)
        while all(p.poll() is None for p in procs.values()):
            time.sleep(1)
        print("a process exited; see run/*.log", flush=True)
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        stop(procs)


if __name__ == "__main__":
    main()
