"""A machine that fills the task system's roles for its tests, speaking only the assignment
protocol on the bus (tasking/assignments.py): it accepts what is offered to its roles, does the
work, and reports. What the scheduler does with the answers is under test, not who gives them.

- worker: in a task's folder it writes RESULT.md; in a code worktree it adds a line to WORK.md
  and commits. ``block`` reports BLOCKED instead; ``block_on_rejection`` reports BLOCKED when
  the offer says the ticket was rejected (what a worker that cannot act on feedback does);
  ``break_first`` also deletes README.md on the first attempt (the unit command checks for it)
  and restores it on the next.
- reviewer: APPROVED, or REJECTED the first time when ``reject_first``, or every time when
  ``always_reject``; it names the offer's ``review.commit`` in its report unless ``echo_commit``
  is false (a reviewer that omits the commit).
- ``instance`` names it (``worker/<instance>``); ``claims`` is what it says its instance is in
  task.accepted when that should differ from its source. With ``heartbeat`` set it works
  slowly: after accepting it publishes task.working every that many seconds and reports only
  once ``finish`` is set; a task.released naming its assignment (and it, or nobody in
  particular) stops it."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from tasking.assignments import ACCEPTED, ASSIGNED, RELEASED, REPORTED, WORKING


class FakeMachine:
    def __init__(self, events, roles=("worker", "reviewer"), *, instance="1", claims: str | None = None, block=False,
                 block_on_rejection=False, break_first=False, reject_first=False, always_reject=False, heartbeat: float | None = None,
                 echo_commit=True):
        self.events, self.roles, self.instance, self.claims = events, tuple(roles), instance, claims
        self.block, self.block_on_rejection, self.break_first = block, block_on_rejection, break_first
        self.reject_first, self.always_reject, self.heartbeat, self.echo_commit = reject_first, always_reject, heartbeat, echo_commit
        self.offers: list[dict] = []
        self.finish = threading.Event()
        self._broke: set[str] = set()
        self._rejected: set[str] = set()
        self._released: set[str] = set()
        self._stop = threading.Event()
        self._offset = 0
        self._t = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.finish.set()
        self._t.join(timeout=5)

    def _me(self, role: str) -> str:
        return f"{role}/{self.instance}"

    def _run(self):
        while not self._stop.wait(0.05):
            evs, self._offset = self.events.after(self._offset)
            for ev in evs:
                if ev.get("type") == ASSIGNED and ev.get("role") in self.roles:
                    self.offers.append(ev)
                    threading.Thread(target=self._answer, args=(ev,), daemon=True).start()
                elif ev.get("type") == RELEASED and ev.get("instance") in (None, self._me(ev.get("role", ""))):
                    self._released.add(ev.get("assignment"))

    def _answer(self, a: dict) -> None:
        role, tid, aid = a["role"], a["ticket"], a["assignment"]
        inst = self._me(role)
        say = lambda kind, **d: self.events.publish(kind, {"assignment": aid, "ticket": tid, "role": role, **d}, source=inst, subject=tid)
        say(ACCEPTED, instance=self.claims or inst)
        if self.heartbeat:
            while not self.finish.wait(self.heartbeat):
                if aid in self._released or self._stop.is_set():
                    return
                say(WORKING)
        if aid in self._released:
            return
        report, commit = (self._work if role == "worker" else self._review)(a, Path(a["workspace"]))
        say(REPORTED, report=report, failed=False, text="done", commit=commit)

    def _work(self, a: dict, cwd: Path):
        tid = a["ticket"]
        if self.block:
            return {"state": "BLOCKED", "summary": "cannot", "blocked_reason": "need a decision"}, None
        if self.block_on_rejection and (a.get("brief") or {}).get("rejected_by"):
            return {"state": "BLOCKED", "summary": "cannot act on the feedback", "blocked_reason": "this worker cannot act on review feedback"}, None
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
        commit = (a.get("review") or {}).get("commit") if self.echo_commit else None
        if self.always_reject or (self.reject_first and a["ticket"] not in self._rejected):
            self._rejected.add(a["ticket"])
            return {"verdict": "REJECTED", "summary": "needs work", "feedback": "1. add more"}, commit
        return {"verdict": "APPROVED", "summary": "looks good"}, commit
