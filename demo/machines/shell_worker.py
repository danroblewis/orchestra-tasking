"""A worker that is a program: it does tasks that are shell commands.

It fills the task system's ``worker`` role by speaking the assignment protocol on Orchestra's bus
(orchestra-tasking: tasking/assignments.py), using nothing but Orchestra's library. On
``task.assigned`` for a worker it accepts, runs the commands the ticket's description gives in
fenced ``sh`` blocks, in order, in the ticket's workspace, says it is still working while they
run, and reports READY_FOR_REVIEW:

- a task (the offer names no branch): the workspace is a folder of the ticket's own, empty at
  first; nothing of the repository is there, and ``ORCHESTRA_REPO`` names the repository for a
  command that wants it (``cd "$ORCHESTRA_REPO" && …``). What the commands printed goes to
  RESULT.md there;
- code (the offer names a branch): the workspace is a git worktree of the repository on that
  branch, so the commands change the repository's files where they are; the worker commits what
  they changed, with the commands and their exit codes in the commit message, for the reviewer
  and the merge gate. Nothing else is written into the worktree.

It reports BLOCKED when the ticket gives no commands, or was rejected and running the same
commands again could only give the same result: a program cannot act on a reviewer's words, so it
hands the decision back, with the reviewer's words in the reason. With ``--only-commands`` it
does not take such a ticket at all: it leaves the offer for another machine serving the role (an
AI worker, say) to take; use it whenever something else serves the role too. A ``task.released``
for its assignment stops the command running, the ones after it, the heartbeat and the report:
nothing more is written or said for it.

    python machines/shell_worker.py [instance] [--only-commands]   # the machine ``worker``, instance 1 by default
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from orchestra.config import load_project
from orchestra.events import Events

ROLE = "worker"
ASSIGNED, ACCEPTED, WORKING, REPORTED, RELEASED = "task.assigned", "task.accepted", "task.working", "task.reported", "task.released"
COMMAND_TIMEOUT_S = 300
BLOCK = re.compile(r"```(?:sh|bash|shell)\s*\n(.*?)```", re.DOTALL)


def commands(brief: dict) -> list[str]:
    return [c.strip() for c in BLOCK.findall(brief.get("description") or "") if c.strip()]


def ran_before_file(offer: dict, env: dict[str, str] | None = None) -> Path:
    """Where the commands of the last turn are kept: in the folder of a task; in code, inside the
    worktree's own git directory, so that it is never committed. ``env``: what git runs with."""
    folder = Path(offer["workspace"])
    if offer.get("branch") is None:
        return folder / ".commands"
    git_path = subprocess.run(["git", "rev-parse", "--git-path", "shell-worker-commands"], cwd=folder, env=env,
                              capture_output=True, text=True, check=True).stdout.strip()
    return folder / git_path


def same_again(offer: dict, cmds: list[str], env: dict[str, str] | None = None) -> bool:
    """Rejected, and these are the commands the last turn ran: they would give the same result."""
    if not (offer.get("brief") or {}).get("rejected_by"):
        return False
    try:
        f = ran_before_file(offer, env)
    except (OSError, subprocess.CalledProcessError):
        return False
    return f.exists() and f.read_text() == "\n".join(cmds)


def can_do(offer: dict, env: dict[str, str] | None = None) -> bool:
    """What this worker can move: a ticket with commands, unless it would only run them again."""
    cmds = commands(offer.get("brief") or {})
    return bool(cmds) and not same_again(offer, cmds, env)


def main(instance: str, only_commands: bool) -> None:
    me = f"{ROLE}/{instance}"
    project = load_project()
    events = Events.for_project(project, source=me)
    env = {**os.environ, "ORCHESTRA_REPO": str(project.repo_root)}   # what every command runs with
    # ORCHESTRA_MACHINE_LOG: where `orchestra run` sends this program's output when it runs it
    # ([[machines]] in orchestra.toml), so Orchestra's machine dialog can show it
    log = os.environ.get("ORCHESTRA_MACHINE_LOG")
    events.publish("orchestra.machine.started", {"mode": "continuous", "kind": "program", "listens_for": [ASSIGNED, RELEASED],
                                                 **({"log": log} if log else {})})
    print(f"{me}: waiting for {ROLE} assignments" + (" with commands (--only-commands)" if only_commands else ""), flush=True)
    offset, jobs = events.end(), {}                      # assignment id -> the Job doing it
    while True:
        evs, offset = events.after(offset)
        for ev in evs:
            d = ev.get("data") or {}
            if ev.get("type") == ASSIGNED and d.get("role") == ROLE:
                if only_commands and not can_do(d, env):
                    print(f"{me}: {d.get('ticket')} not taken: no `sh` block, or only the commands a rejection already "
                          "answered; left for another worker", flush=True)
                    continue
                jobs[d["assignment"]] = Job(events, me, d, jobs, env)
            elif ev.get("type") == RELEASED and d.get("assignment") in jobs and d.get("instance") in (None, me):
                jobs[d["assignment"]].stop()            # the lease ran out, the operator stopped it, or another holds it
        time.sleep(0.5)


class Job:
    def __init__(self, events: Events, me: str, offer: dict, jobs: dict, env: dict[str, str]):
        self.events, self.me, self.offer, self.jobs, self.env = events, me, offer, jobs, env
        self.folder = Path(offer["workspace"])
        self.released, self.done = threading.Event(), threading.Event()
        self.proc: subprocess.Popen | None = None
        threading.Thread(target=self.run, daemon=True).start()

    def say(self, kind: str, **data) -> None:
        self.events.publish(kind, {"assignment": self.offer["assignment"], "ticket": self.offer["ticket"], "role": ROLE, **data},
                            subject=self.offer["ticket"])

    def stop(self) -> None:
        self.released.set()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()

    def run(self) -> None:
        self.say(ACCEPTED, instance=self.me)
        threading.Thread(target=self.heartbeat, daemon=True).start()
        try:
            report = self.work()
            if report is not None and not self.released.is_set():
                self.say(REPORTED, report=report, failed=False, text="")
                print(f"{self.me}: {self.offer['ticket']} {report['state']}", flush=True)
        finally:
            self.done.set()
            self.jobs.pop(self.offer["assignment"], None)

    def heartbeat(self) -> None:
        """Six heartbeats a lease (the offer's ``lease_s``), so one lost on the way loses nothing."""
        while not self.done.wait(self.offer["lease_s"] / 6):
            if self.released.is_set():
                return
            self.say(WORKING)

    def work(self) -> dict | None:
        """The report, or None when the assignment was released meanwhile."""
        brief, tid = self.offer.get("brief") or {}, self.offer["ticket"]
        cmds = commands(brief)
        if not cmds:
            return {"state": "BLOCKED", "summary": "the ticket gives no commands",
                    "blocked_reason": "The ticket gives no commands: this worker runs the `sh` code blocks of a ticket's description, "
                                      "and there are none. Commands in the description, or a worker that can act on prose, would move it."}
        if same_again(self.offer, cmds, self.env):
            feedback = (brief.get("feedback") or "").strip()
            first = feedback.splitlines()[0].lstrip("-* ").rstrip(".") if feedback else "no feedback was given"
            quoted = "\n".join(f"> {line}" for line in feedback[:600].splitlines())
            return {"state": "BLOCKED", "summary": "rejected, and the same commands would give the same result",
                    "blocked_reason": "The reviewer wants what these commands never produce, and this worker cannot change commands "
                                      f"on feedback: {first}. Changed commands or acceptance criteria, then an unblock, would move it.\n\n{quoted}"}
        code = self.offer.get("branch") is not None
        lines = [f"# {tid}: {brief.get('title', '')}", "",
                 f"Ran in {self.folder} " + (f"(the worktree of branch {self.offer['branch']})." if code
                                             else f"(ORCHESTRA_REPO={self.env['ORCHESTRA_REPO']})."), ""]
        codes, last = [], ""
        for c in cmds:
            if self.released.is_set():
                return None
            self.proc = subprocess.Popen(["bash", "-c", c], cwd=self.folder, env=self.env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if self.released.is_set():                  # released while it started: stop() saw no process
                self.proc.kill()
            try:
                last, _ = self.proc.communicate(timeout=COMMAND_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                last = (self.proc.communicate()[0] or "") + f"\n(stopped after {COMMAND_TIMEOUT_S} s)"
            codes.append(self.proc.returncode)
            lines += ["```sh", c, "```", f"exit code {self.proc.returncode}", "", "```", (last or "").rstrip()[-8000:], "```", ""]
        if self.released.is_set():
            return None
        printed = (last or "").strip()[-600:] or "(nothing)"
        if code:
            outcome = self.commit(f"{tid}: {brief.get('title', '')}\n\n" + "\n".join(lines[2:]))
        else:
            (self.folder / "RESULT.md").write_text("\n".join(lines), encoding="utf-8")
            outcome = "The commands and their full output are in RESULT.md."
        ran_before_file(self.offer, self.env).write_text("\n".join(cmds), encoding="utf-8")
        return {"state": "READY_FOR_REVIEW",
                "summary": f"Ran {len(cmds)} command(s), exit codes {codes}. The last printed: {printed}. {outcome}"}

    def commit(self, record: str) -> str:
        """Commit what the commands changed in the worktree, the record of the run as the message
        (its first line the ticket and title); what to say about it in the report."""
        git, env = ["git", "-C", str(self.folder)], self.env       # git runs with what the commands ran with
        if not subprocess.run([*git, "config", "user.email"], env=env, capture_output=True, check=False).stdout.strip():
            git += ["-c", f"user.name={self.me}", "-c", f"user.email={self.me.replace('/', '-')}@orchestra.invalid"]
        subprocess.run([*git, "add", "-A"], env=env, check=True)
        if subprocess.run([*git, "diff", "--cached", "--quiet"], env=env, check=False).returncode == 0:
            return "They changed nothing in the worktree, so there is nothing to commit."
        subprocess.run([*git, "commit", "-q", "--cleanup=verbatim", "-F", "-"], env=env, input=record, text=True, check=True)
        head = subprocess.run([*git, "rev-parse", "--short", "HEAD"], env=env, capture_output=True, text=True, check=True).stdout.strip()
        return f"Committed what they changed as {head} on {self.offer['branch']}; the commit message has the commands and their output."


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("instance", nargs="?", default="1")
    ap.add_argument("--only-commands", action="store_true",
                    help="take only tickets with `sh` blocks it would not just run again; leave the rest for another worker")
    a = ap.parse_args()
    try:
        main(a.instance, a.only_commands)
    except KeyboardInterrupt:
        pass
