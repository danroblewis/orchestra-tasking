"""A machine that fills the task system's roles for its tests, speaking only the assignment
protocol on the bus (tasking/assignments.py): it accepts what is offered to its roles, does the
work, and reports. What the scheduler does with the answers is under test, not who gives them.

- worker: in a task's folder it writes RESULT.md; in a code worktree it adds a line to WORK.md
  and commits. ``block`` reports BLOCKED instead; ``break_first`` also deletes README.md on the
  first attempt (the e2e unit command checks for it) and restores it on the next.
- reviewer: APPROVED, or REJECTED the first time when ``reject_first``."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from tasking.assignments import ACCEPTED, ASSIGNED, REPORTED, Follower


class FakeMachine:
    def __init__(self, events, roles=("worker", "reviewer"), *, block=False, break_first=False, reject_first=False):
        self.events, self.roles = events, tuple(roles)
        self.block, self.break_first, self.reject_first = block, break_first, reject_first
        self.offers: list[dict] = []
        self._broke: set[str] = set()
        self._rejected: set[str] = set()
        self._stop = threading.Event()
        self._follow = Follower(events, start=0)
        self._t = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=5)

    def _run(self):
        while not self._stop.wait(0.05):
            for ev in self._follow.new():
                if ev.get("type") == ASSIGNED and ev.get("role") in self.roles:
                    self.offers.append(ev)
                    self._answer(ev)

    def _answer(self, a: dict) -> None:
        role, tid = a["role"], a["ticket"]
        inst = f"{role}/1"
        say = lambda kind, **d: self.events.publish(kind, {"assignment": a["assignment"], "ticket": tid, "role": role, **d},
                                                    source=inst, subject=tid)
        say(ACCEPTED, instance=inst)
        report, commit = (self._work if role == "worker" else self._review)(a, Path(a["workspace"]))
        say(REPORTED, report=report, failed=False, text="done", commit=commit)

    def _work(self, a: dict, cwd: Path):
        tid = a["ticket"]
        if self.block:
            return {"state": "BLOCKED", "summary": "cannot", "blocked_reason": "need a decision"}, None
        if a.get("branch") is None:                                    # a task
            (cwd / "RESULT.md").write_text(f"# {tid}\n\nthe result\n")
            return {"state": "READY_FOR_REVIEW", "summary": "wrote RESULT.md"}, None
        f = cwd / "WORK.md"
        f.write_text((f.read_text() if f.exists() else "") + f"work on {tid}\n")
        if self.break_first and tid not in self._broke:
            self._broke.add(tid)
            (cwd / "README.md").unlink()
        elif tid in self._broke and not (cwd / "README.md").exists():
            (cwd / "README.md").write_text("# e2e\n")
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-qm", f"{tid}: fake work"], cwd=cwd, check=True)
        return {"state": "READY_FOR_REVIEW", "summary": "added WORK.md line"}, None

    def _review(self, a: dict, cwd: Path):
        commit = (a.get("review") or {}).get("commit")
        if self.reject_first and a["ticket"] not in self._rejected:
            self._rejected.add(a["ticket"])
            return {"verdict": "REJECTED", "summary": "needs work", "feedback": "1. add more"}, commit
        return {"verdict": "APPROVED", "summary": "looks good"}, commit
