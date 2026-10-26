"""How a ticket moves from what came back for an assignment, or did not: a worker's report, a
reviewer's verdict, a report the task system cannot use, a machine that went quiet, an offer
nobody took, the operator's stop. The scheduler (tasking/scheduler.py) decides when; this decides
what the ticket becomes. SPEC.md section 4."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .config import Project
from .events import SCHEDULER, Events
from .git import Git
from .model import RECOVERY, Note, State, Ticket
from .store import LocalStore

CRASH_BUDGET = 2          # worker turns that may end without a report before the ticket is blocked
NO_VERDICT_BUDGET = 3     # reviews that may end without a verdict before the ticket is sent back


def unusable(role: str, report: dict | None) -> str | None:
    """Why a report cannot be applied, or None. No report at all is usable: it means the turn ended
    without one and is handled as such."""
    if report is None:
        return None
    if not isinstance(report, dict):
        return "the report was not an object"
    if role == "worker":
        state = report.get("state")
        if state == "READY_FOR_REVIEW" or (state == "BLOCKED" and report.get("blocked_reason")):
            return None
        if state == "BLOCKED":
            return "the report said BLOCKED without a blocked_reason"
        return "the report had no state" if state is None else f"the report's state {state!r} is not READY_FOR_REVIEW or BLOCKED"
    verdict = report.get("verdict")
    if verdict in ("APPROVED", "REJECTED"):
        return None
    return "the report had no verdict" if verdict is None else f"the report's verdict {verdict!r} is not APPROVED or REJECTED"


class Reports:
    def __init__(self, project: Project, store: LocalStore, events: Events, git: Git, log: Callable[[str], None]):
        self.project, self.store, self.events, self.git, self.log = project, store, events, git, log

    @property
    def cfg(self):
        return self.project.config

    def apply(self, a: dict, report: dict | None, text: str, failed: bool, commit: str | None) -> None:
        """What an assignment's machine reported (or, ``report`` None, that it ended without one;
        ``failed`` when it went away or its lease ran out), applied to the ticket. A report the task
        system cannot use counts as none, with what was wrong as the text."""
        why = unusable(a["role"], report)
        if why:
            self.log(f"{a['ticket']}: the {a['role']}'s report is unusable: {why}")
            report, text, failed = None, why, True
        if a["role"] == "worker":
            self._worker(a["ticket"], report, text, failed, Path(a["workspace"]) if a.get("workspace") else None, a.get("branch"))
        else:
            self._review(a["ticket"], report, text, failed, commit)

    def unanswered(self, a: dict) -> None:
        """An offer no machine accepted: the ticket goes back to waiting to be offered again, and
        nothing is counted against anyone (the worker turn the offer took is given back)."""
        t = self.store.get_ticket(a["ticket"])
        if t.state not in RECOVERY:
            return
        turns = {"worker_turns": t.worker_turns - 1} if a["role"] == "worker" else {}
        self.store.transition(t.id, t.version, RECOVERY[t.state], **turns)
        self.log(f"{t.id}: the offer to a {a['role']} was not taken; back to {RECOVERY[t.state]}")

    def stopped(self, a: dict) -> None:
        """The operator stopped the machine: the ticket goes back to waiting without counting."""
        role = a["role"]
        self.back_to_waiting(a["ticket"], f"the {role} was stopped by the operator",
                             "the previous worker's turn was stopped by the operator; the workspace is as it left it" if role == "worker" else None)

    def back_to_waiting(self, tid: str, note: str, feedback: str | None = None) -> None:
        """An active ticket to the waiting state before it, with a note saying why (and, for a
        worker's turn, what the next worker is told)."""
        t = self.store.get_ticket(tid)
        if t.state not in RECOVERY:
            return
        self.store.add_note(tid, Note(author=SCHEDULER, text=f"{note}; back to {RECOVERY[t.state]}"))
        fields = {"feedback": feedback} if feedback and t.state == State.IN_PROGRESS else {}
        self.store.transition(tid, t.version, RECOVERY[t.state], **fields)
        self.log(f"{tid}: {t.state} -> {RECOVERY[t.state]} ({note})")

    def _trailing(self, tid: str, who: str) -> int:
        """How many of the latest notes in a row are ``who``'s crash notes: any other note (an
        unblock, a report) resets the budget instead of a ticket carrying old crashes forever."""
        n = 0
        for note in reversed(self.store.notes(tid)):
            if note.get("kind") != "crash" or not note.get("text", "").startswith(f"the {who}"):
                break
            n += 1
        return n

    def _worker(self, tid: str, report: dict | None, text: str, failed: bool, path: Path | None, branch: str | None) -> None:
        t = self.store.get_ticket(tid)
        if t.state != State.IN_PROGRESS:
            return
        if report is None:
            crashes = (1 if failed else 0) + self._trailing(tid, "worker")
            if failed and crashes <= CRASH_BUDGET:
                # the worker went away or ran out its lease: not a decision for a human; the
                # workspace is kept and the ticket offered again
                self.store.add_note(tid, Note(author=SCHEDULER, kind="crash",
                                              text=f"the worker ended without reporting (attempt {crashes}){': ' + text[-300:] if text else ''}; offered again"))
                self.store.transition(tid, t.version, State.OPEN,
                                      feedback="the previous worker ended without reporting; the workspace is as it left it")
                self.log(f"{tid}: the worker ended without reporting; back to OPEN ({crashes})")
                return
            reason = "the worker went away or ran out its lease repeatedly" if failed else "the worker finished without a report"
            reason += f". Last message: {text[-600:]}" if text else "."
            self._block(t, reason)
            return
        self.store.add_note(tid, Note(author="worker", text=report.get("summary", "")))
        if report["state"] == "BLOCKED":
            self.store.transition(tid, t.version, State.BLOCKED, blocked_reason=report["blocked_reason"])
            self.events.attention("coordinator", f"{tid} BLOCKED by worker: {report['blocked_reason'][:200]}", ticket=tid)
            self.log(f"{tid}: BLOCKED by worker")
            return
        if branch is None:                                   # a task: its result is the report and the folder
            self.store.transition(tid, t.version, State.READY_FOR_REVIEW, feedback=None)
            self.log(f"{tid}: READY_FOR_REVIEW")
            return
        # code: make sure there is something to review
        if self.git.run("status", "--porcelain", cwd=path, check=False).stdout.strip():
            self.git.run("add", "-A", cwd=path, check=False)
            self.git.run("commit", "-q", "-m", f"{tid}: commit remaining worker changes", cwd=path, check=False)
        head = self.git.rev_parse("HEAD", cwd=path)
        if head == self.git.rev_parse(self.cfg.base_branch) or self.git.is_ancestor(head, self.cfg.base_branch):
            self._block(t, "the worker reported READY_FOR_REVIEW but the branch has no commits beyond the base branch")
            return
        self.store.transition(tid, t.version, State.READY_FOR_REVIEW, reviewed_commit=head, feedback=None)
        self.log(f"{tid}: READY_FOR_REVIEW at {head[:10]}")

    def _block(self, t: Ticket, reason: str) -> None:
        self.store.add_note(t.id, Note(author=SCHEDULER, kind="blocker", text=reason))
        self.store.transition(t.id, t.version, State.BLOCKED, blocked_reason=reason)
        self.events.attention("coordinator", f"{t.id} BLOCKED: {reason[:200]}", ticket=t.id)
        self.log(f"{t.id}: BLOCKED ({reason[:80]})")

    def _review(self, tid: str, report: dict | None, text: str, failed: bool, commit: str | None) -> None:
        """A reviewer's report: its verdict, or (none came) the review is redone, up to
        NO_VERDICT_BUDGET times in a row, before the ticket goes back to its worker."""
        t = self.store.get_ticket(tid)
        if t.state != State.IN_REVIEW:
            return
        if report is None:
            # no verdict is not a rejection: the worker has nothing to fix. The send-back after the
            # budget is the task system's, never counted as a reviewer's verdict
            tries = 1 + self._trailing(tid, "reviewer")
            if tries <= NO_VERDICT_BUDGET:
                self.store.add_note(tid, Note(author=SCHEDULER, kind="crash",
                                              text=f"the reviewer ended without a verdict (attempt {tries}){': ' + text[-300:] if text else ''}; the review will be redone"))
                self.store.transition(tid, t.version, State.READY_FOR_REVIEW)
                self.log(f"{tid}: the reviewer ended without a verdict; the review will be redone ({tries})")
                return
            self.reject(tid, SCHEDULER, f"{NO_VERDICT_BUDGET} reviews in a row ended without a verdict; the work may be too large for a review to finish")
            return
        self.store.add_note(tid, Note(author="reviewer", kind="review",
                                      text=f"{report['verdict']}: {report.get('summary', '')}\n{report.get('feedback') or ''}"))
        if report["verdict"] == "APPROVED":
            t = self.store.transition(tid, t.version, State.APPROVED, reviewed_commit=commit, feedback=None, rejected_by=None)
            self.log(f"{tid}: APPROVED")
            if self.cfg.work != "code":                       # nothing to merge: approved is done
                self.store.transition(tid, t.version, State.DONE)
                self.log(f"{tid}: DONE")
                self.events.attention("coordinator", f"{tid} is DONE", ticket=tid)
        else:
            self.reject(tid, "reviewer", report.get("feedback") or report.get("summary", ""))

    def reject(self, tid: str, by: str, feedback: str) -> None:
        """Sent back to its worker by ``by``: the reviewer, or the task system (``task_scheduler``)
        when reviews keep ending without a verdict or the work cannot be reviewed."""
        t = self.store.get_ticket(tid)
        rounds = t.review_rounds + 1
        self.store.transition(tid, t.version, State.REJECTED, rejected_by=by, feedback=feedback, review_rounds=rounds,
                              reviewer_rejections=t.reviewer_rejections + (1 if by == "reviewer" else 0))
        self.log(f"{tid}: REJECTED by {by} (round {rounds})")
        # the planner is told when the threshold is crossed, and at every further multiple of it,
        # not on every rejection: it should decide once, not be told 38 times
        th = self.cfg.review_rounds_before_escalation
        if th and rounds % th == 0:
            self.events.attention("coordinator", f"{tid} has been rejected {rounds} times; decide whether to narrow, split, or cancel it", ticket=tid)
