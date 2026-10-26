"""A worker that is a program: it does tasks that are shell commands.

It fills the task system's ``worker`` role by speaking the assignment protocol on Orchestra's bus
(orchestra-tasking: tasking/assignments.py), using nothing but Orchestra's library. On
``task.assigned`` for a worker it accepts, runs the commands the ticket's description gives in
fenced ``sh`` blocks, in the ticket's workspace (a folder of the ticket's own, empty at first;
nothing of the repository is there, and ``ORCHESTRA_REPO`` names the repository for a command
that wants it: ``cd "$ORCHESTRA_REPO" && …``), writes what they printed to RESULT.md, says it is
still working while they run, and reports READY_FOR_REVIEW. It reports BLOCKED when the ticket
gives no commands, or was rejected and running the same commands again could only give the same
result: a program cannot act on a reviewer's words, so it hands the decision back, with the
reviewer's words in the reason. A ``task.released`` for its assignment stops the command running,
the ones after it, the heartbeat and the report: nothing more is written or said for it.

    python machines/shell_worker.py [instance]      # the machine ``worker``, instance 1 by default
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from orchestra.config import load_project
from orchestra.events import Events

ROLE = "worker"
ASSIGNED, ACCEPTED, WORKING, REPORTED, RELEASED = "task.assigned", "task.accepted", "task.working", "task.reported", "task.released"
COMMAND_TIMEOUT_S = 300
BLOCK = re.compile(r"```(?:sh|bash|shell)\s*\n(.*?)```", re.DOTALL)


def main(instance: str) -> None:
    me = f"{ROLE}/{instance}"
    project = load_project()
    events = Events.for_project(project, source=me)
    env = {**os.environ, "ORCHESTRA_REPO": str(project.repo_root)}   # what every command runs with
    # ORCHESTRA_LOG: where whoever started this program sends its output (demo/run.py: run/worker.log
    # in the project), so Orchestra's machine dialog can show it
    events.publish("orchestra.machine.started", {"mode": "continuous", "kind": "program", "listens_for": [ASSIGNED, RELEASED],
                                                 **({"log": os.environ["ORCHESTRA_LOG"]} if os.environ.get("ORCHESTRA_LOG") else {})})
    print(f"{me}: waiting for {ROLE} assignments", flush=True)
    offset, jobs = events.end(), {}                      # assignment id -> the Job doing it
    while True:
        evs, offset = events.after(offset)
        for ev in evs:
            d = ev.get("data") or {}
            if ev.get("type") == ASSIGNED and d.get("role") == ROLE:
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
        commands = [c.strip() for c in BLOCK.findall(brief.get("description") or "") if c.strip()]
        ran_before = self.folder / ".commands"
        if not commands:
            return {"state": "BLOCKED", "summary": "the ticket gives no commands",
                    "blocked_reason": "The ticket gives no commands: this worker runs the `sh` code blocks of a ticket's description, "
                                      "and there are none. Commands in the description, or a worker that can act on prose, would move it."}
        if brief.get("rejected_by") and ran_before.exists() and ran_before.read_text() == "\n".join(commands):
            feedback = (brief.get("feedback") or "").strip()
            first = feedback.splitlines()[0].lstrip("-* ").rstrip(".") if feedback else "no feedback was given"
            quoted = "\n".join(f"> {line}" for line in feedback[:600].splitlines())
            return {"state": "BLOCKED", "summary": "rejected, and the same commands would give the same result",
                    "blocked_reason": "The reviewer wants what these commands never produce, and this worker cannot change commands "
                                      f"on feedback: {first}. Changed commands or acceptance criteria, then an unblock, would move it.\n\n{quoted}"}
        lines = [f"# {tid}: {brief.get('title', '')}", "", f"Ran in {self.folder} (ORCHESTRA_REPO={self.env['ORCHESTRA_REPO']}).", ""]
        codes, last = [], ""
        for c in commands:
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
        (self.folder / "RESULT.md").write_text("\n".join(lines), encoding="utf-8")
        ran_before.write_text("\n".join(commands), encoding="utf-8")
        return {"state": "READY_FOR_REVIEW",
                "summary": f"Ran {len(commands)} command(s), exit codes {codes}. The last printed: {(last or '').strip()[-600:] or '(nothing)'}. "
                           "The commands and their full output are in RESULT.md."}


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "1")
    except KeyboardInterrupt:
        pass
