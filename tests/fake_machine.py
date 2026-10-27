"""A machine that fills the task system's roles for its tests, speaking only the protocol on the
bus (tasking/assignments.py): it claims what is offered to its roles (``task.assigned`` from the
queue, ``review.requested`` from a worker, ``changes.requested`` addressed to it), starts only
once the bus shows its claim was the first, does the work, and passes it on directly: a worker
asks the reviewers (``review.requested``), a reviewer asks the worker for changes
(``changes.requested``) or approves (``review.approved``). What the scheduler records of it is
under test, not who gives the answers.

- worker: in a task's folder it writes RESULT.md; in a code worktree it adds a line to WORK.md,
  commits, and runs the offer's unit command for the reviewer. ``block`` reports BLOCKED
  instead; ``block_on_rejection`` reports BLOCKED when the offer says the ticket was rejected
  (what a worker that cannot act on feedback does); ``break_first`` also deletes README.md on the
  first attempt (the unit command checks for it) and restores it on the next; ``leave_after``
  stops the machine once it has asked for a review (a worker that is gone when the rejection
  comes back).
- reviewer: APPROVED, or REJECTED the first time when ``reject_first``, or every time when
  ``always_reject``; it names the offer's ``review.commit`` in its verdict unless ``echo_commit``
  is false (a reviewer that omits the commit). ``litter`` leaves files a reviewer's run makes in a
  code worktree (an untracked ``LITTER.txt`` and an ignored ``cache/<ticket>``, when
  ``.gitignore`` lists ``cache/``), and removes the untracked one when it passes the workspace on
  to the worker, as a machine must (an approval passes no workspace: the gate takes its own).
- ``instance`` names it (``worker/<instance>``); ``claims`` is what it says its instance is in
  task.accepted when that should differ from its source. With ``heartbeat`` set it works
  slowly: once it holds the turn it publishes task.working every that many seconds and ends the
  turn only once ``finish`` is set; a task.released naming its assignment (and it, or nobody in
  particular) stops it.
- ``look`` is called with each offer and its workspace before the work, so a test can see what
  the machine was handed. ``started`` lists the (assignment, instance) of every turn it began
  work on, ``lost`` the claims another instance was first to."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from tasking.assignments import (
    ACCEPTED,
    APPROVED,
    CHANGES_REQUESTED,
    OFFERS,
    RELEASED,
    REPORTED,
    REVIEW_REQUESTED,
    WORKING,
    holder,
    pass_on,
)


class FakeMachine:
    def __init__(self, events, roles=("worker", "reviewer"), *, instance="1", claims: str | None = None, block=False,
                 block_on_rejection=False, break_first=False, reject_first=False, always_reject=False, heartbeat: float | None = None,
                 echo_commit=True, look=None, litter=False, leave_after=False):
        self.events, self.roles, self.instance, self.claims = events, tuple(roles), instance, claims
        self.block, self.block_on_rejection, self.break_first = block, block_on_rejection, break_first
        self.reject_first, self.always_reject, self.heartbeat, self.echo_commit = reject_first, always_reject, heartbeat, echo_commit
        self.look, self.litter, self.leave_after = look, litter, leave_after
        self.offers: list[dict] = []
        self.started: list[tuple[str, str]] = []
        self.lost: list[str] = []
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
            start = self._offset
            evs, self._offset = self.events.after(self._offset)
            for ev in evs:
                if ev.get("type") in OFFERS and ev.get("role") in self.roles and ev.get("to") in (None, self._me(ev["role"])):
                    self.offers.append(ev)
                    threading.Thread(target=self._answer, args=(ev, start), daemon=True).start()
                elif ev.get("type") == RELEASED and ev.get("instance") in (None, self._me(ev.get("role", ""))):
                    self._released.add(ev.get("assignment"))

    def _held(self, a: dict, claim: dict, start: int) -> bool:
        """Whether this instance holds the turn: its claim, read back from the bus, is the first
        valid one of the offer (a release of it, or naming it, says no)."""
        end = time.time() + 10
        while time.time() < end and not self._stop.is_set():
            evs, _ = self.events.after(start)
            ids = [e.get("id") for e in evs]
            if claim["id"] in ids:
                return a["assignment"] not in self._released and holder(evs[:ids.index(claim["id"]) + 1], a) == claim["source"]
            time.sleep(0.02)
        return False

    def _answer(self, a: dict, start: int) -> None:
        role, tid, aid = a["role"], a["ticket"], a["assignment"]
        inst = self._me(role)
        say = lambda kind, **d: self.events.publish(kind, d, source=inst, subject=tid, causationid=a.get("id"))
        claim = say(ACCEPTED, assignment=aid, ticket=tid, role=role, instance=self.claims or inst)
        if not self._held(a, claim, start):
            self.lost.append(aid)
            return
        self.started.append((aid, inst))
        if self.heartbeat:
            while not self.finish.wait(self.heartbeat):
                if aid in self._released or self._stop.is_set():
                    return
                say(WORKING, assignment=aid, ticket=tid, role=role)
        if aid in self._released:
            return
        if self.look:
            self.look(a, Path(a["workspace"]))
        if role == "worker":
            self._work(a, Path(a["workspace"]), say)
        else:
            self._review(a, Path(a["workspace"]), say)

    def _work(self, a: dict, cwd: Path, say) -> None:
        tid, me = a["ticket"], self._me("worker")
        blocked = None
        if self.block:
            blocked = {"state": "BLOCKED", "summary": "cannot", "blocked_reason": "need a decision"}
        elif self.block_on_rejection and (a.get("brief") or {}).get("rejected_by"):
            blocked = {"state": "BLOCKED", "summary": "cannot act on the feedback", "blocked_reason": "this worker cannot act on review feedback"}
        if blocked:
            say(REPORTED, assignment=a["assignment"], ticket=tid, role="worker", report=blocked, failed=False, text="done")
            return
        if a.get("branch") is None:                                    # a task
            (cwd / "RESULT.md").write_text(f"# {tid}\n\nthe result\n")
            say(REVIEW_REQUESTED, **pass_on(a, REVIEW_REQUESTED, source=me,
                                            review={"summary": "wrote RESULT.md", "commit": None, "test_output": None, "worker": me}))
        else:
            f = cwd / "WORK.md"
            f.write_text((f.read_text() if f.exists() else "") + f"work on {tid}\n")
            if self.break_first and tid not in self._broke:
                self._broke.add(tid)
                (cwd / "README.md").unlink()
            elif tid in self._broke and not (cwd / "README.md").exists():
                (cwd / "README.md").write_text("# e2e\n")
            subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
            subprocess.run(["git", "commit", "-qm", f"{tid}: fake work"], cwd=cwd, check=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
            tests = None
            if a.get("unit_command"):
                r = subprocess.run(["bash", "-c", a["unit_command"]], cwd=cwd, capture_output=True, text=True, check=False)
                tests = f"$ {a['unit_command']}\n{r.stdout}{r.stderr}\n[{'passed' if r.returncode == 0 else 'failed'}]"
            say(REVIEW_REQUESTED, **pass_on(a, REVIEW_REQUESTED, source=me,
                                            review={"summary": "added WORK.md line", "commit": head, "test_output": tests, "worker": me}))
        if self.leave_after:
            self._stop.set()

    def _review(self, a: dict, cwd: Path, say) -> None:
        tid, me = a["ticket"], self._me("reviewer")
        if self.litter and a.get("branch"):
            (cwd / "LITTER.txt").write_text("left by the review\n")
            (cwd / "cache").mkdir(exist_ok=True)
            (cwd / "cache" / tid).write_text("warm\n")
        commit = (a.get("review") or {}).get("commit") if self.echo_commit else None
        if self.always_reject or (self.reject_first and tid not in self._rejected):
            self._rejected.add(tid)
            if self.litter and a.get("branch"):
                (cwd / "LITTER.txt").unlink()                        # the workspace passes on: left at the commit it names
            say(CHANGES_REQUESTED, **pass_on(a, CHANGES_REQUESTED, source=me, summary="needs work", feedback="1. add more", commit=commit))
            return
        say(APPROVED, ends=a["assignment"], ticket=tid, role="reviewer", summary="looks good", commit=commit)
