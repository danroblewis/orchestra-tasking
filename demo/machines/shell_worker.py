"""A worker that is a program: it does tasks that are shell commands.

It fills the task system's ``worker`` role by speaking the assignment protocol on Orchestra's bus
(orchestra-tasking: tasking/assignments.py), using nothing but Orchestra's library. On
``task.assigned`` for a worker it accepts, runs the commands the ticket's description gives in
fenced ``sh`` blocks, in the ticket's workspace, writes what they printed to RESULT.md, says it
is still working while they run, and reports READY_FOR_REVIEW. It reports BLOCKED when the
ticket gives no commands, or was rejected and running the same commands again could only give
the same result: a program cannot act on a reviewer's words, so it hands the decision back. A
``task.released`` for its assignment stops the commands.

    python machines/shell_worker.py [instance]      # the machine ``worker``, instance 1 by default
"""

from __future__ import annotations

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
HEARTBEAT_S = 30.0        # the task system's lease is 180 s; a heartbeat keeps it
COMMAND_TIMEOUT_S = 300
BLOCK = re.compile(r"```(?:sh|bash|shell)\s*\n(.*?)```", re.DOTALL)


def main(instance: str) -> None:
    me = f"{ROLE}/{instance}"
    events = Events.for_project(load_project(), source=me)
    events.publish("orchestra.machine.started", {"mode": "continuous", "kind": "program", "listens_for": [ASSIGNED, RELEASED],
                                                 "does": "runs the ```sh blocks of a ticket's description in its workspace"})
    print(f"{me}: waiting for {ROLE} assignments", flush=True)
    offset, jobs = events.end(), {}                      # assignment id -> the Job doing it
    while True:
        end = events.end()
        evs, offset = events.read(offset, limit=end - offset) if end > offset else ([], offset)
        for ev in evs:
            d = ev.get("data") or {}
            if ev.get("type") == ASSIGNED and d.get("role") == ROLE:
                jobs[d["assignment"]] = Job(events, me, d, jobs)
            elif ev.get("type") == RELEASED and d.get("assignment") in jobs and d.get("instance") in (None, me):
                jobs[d["assignment"]].stop()            # the lease ran out, the operator stopped it, or another holds it
        time.sleep(0.5)


class Job:
    def __init__(self, events: Events, me: str, offer: dict, jobs: dict):
        self.events, self.me, self.offer, self.jobs = events, me, offer, jobs
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
            if not self.released.is_set():
                self.say(REPORTED, report=report, failed=False, text="")
                print(f"{self.me}: {self.offer['ticket']} {report['state']}", flush=True)
        finally:
            self.done.set()
            self.jobs.pop(self.offer["assignment"], None)

    def heartbeat(self) -> None:
        while not self.done.wait(HEARTBEAT_S):
            self.say(WORKING)

    def work(self) -> dict:
        brief, tid = self.offer.get("brief") or {}, self.offer["ticket"]
        commands = [c.strip() for c in BLOCK.findall(brief.get("description") or "") if c.strip()]
        ran_before = self.folder / ".commands"
        if not commands:
            return {"state": "BLOCKED", "summary": "the ticket gives no commands",
                    "blocked_reason": "This worker runs the ```sh blocks of a ticket's description, and this ticket has none. "
                                      "Add the commands, or give the ticket to a worker that can do it."}
        if brief.get("rejected_by") and ran_before.exists() and ran_before.read_text() == "\n".join(commands):
            return {"state": "BLOCKED", "summary": "rejected, and the same commands would give the same result",
                    "blocked_reason": f"This worker runs the ticket's commands as written and cannot act on review feedback. "
                                      f"The reviewer said: {(brief.get('feedback') or '')[:600]}  To go on, change the commands or "
                                      "the acceptance criteria, then unblock the ticket."}
        lines, codes, last = [f"# {tid}: {brief.get('title', '')}", ""], [], ""
        for c in commands:
            self.proc = subprocess.Popen(["bash", "-c", c], cwd=self.folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                last, _ = self.proc.communicate(timeout=COMMAND_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                last = (self.proc.communicate()[0] or "") + f"\n(stopped after {COMMAND_TIMEOUT_S} s)"
            codes.append(self.proc.returncode)
            lines += ["```sh", c, "```", f"exit code {self.proc.returncode}", "", "```", (last or "").rstrip()[-8000:], "```", ""]
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
