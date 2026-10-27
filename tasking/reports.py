"""How a ticket moves from what ended a turn, or did not: a worker's review request, a reviewer's
request for changes or approval, a report (blocked, or none), a report the task system cannot
use, a machine that went quiet, the operator's stop. The scheduler (tasking/scheduler.py)
decides when; this decides what the ticket becomes. What another machine did is recorded with
its event as the cause (``cause``), so the record's announcements name it and carry no subject
(tasking/events.py). SPEC.md sections 3 and 4."""

from __future__ import annotations

from collections.abc import Callable

from .assignments import APPROVED
from .config import Project
from .events import COORDINATOR, SCHEDULER, Events
from .git import Git
from .model import RECOVERY, Note, State, Ticket
from .store import LocalStore

CRASH_BUDGET = 2          # worker turns that may end without a report before the ticket is blocked
NO_VERDICT_BUDGET = 3     # reviews that may end without a verdict before the ticket is sent back


def unusable(role: str, report: dict | None) -> str | None:
    """Why a ``task.reported`` cannot be applied, or None. No report at all is usable: it means the
    turn ended without one and is handled as such. A report is only for work that passes to no
    machine: a worker's BLOCKED. Finished work passes on with ``review.requested``, a verdict with
    ``changes.requested`` or ``review.approved`` (tasking/assignments.py)."""
    if report is None:
        return None
    if not isinstance(report, dict):
        return "the report was not an object"
    if role == "worker":
        state = report.get("state")
        if state == "BLOCKED" and report.get("blocked_reason"):
            return None
        if state == "BLOCKED":
            return "the report said BLOCKED without a blocked_reason"
        if state == "READY_FOR_REVIEW":
            return "a worker passes finished work on with review.requested, not task.reported"
        return "the report had no state" if state is None else f"the report's state {state!r} is not BLOCKED"
    return "a reviewer's verdict is changes.requested or review.approved, not task.reported"


def send_back(store: LocalStore, events: Events, threshold: int, t: Ticket, rejected_by: str, feedback: str,
              counted: bool = True, by: str | None = None, cause: dict | None = None, **fields) -> int:
    """The one way a ticket is rejected back to its worker, by a reviewer, the task system or the
    merge gate: the round is counted (unless ``counted`` is False: nothing was reviewed, or a
    merge conflict), and the planner (the coordinator role) is told when the round count reaches
    the threshold and at every further multiple of it, not on every rejection: it should decide
    once, not be told 38 times. ``by`` is the machine named in the metrics (the writer's own when
    None). Returns the ticket's review rounds."""
    rounds = t.review_rounds + (1 if counted else 0)
    store.transition(t.id, t.version, State.REJECTED, by=by, cause=cause, rejected_by=rejected_by, feedback=feedback,
                     review_rounds=rounds, **fields)
    if counted and threshold and rounds % threshold == 0:
        events.attention(COORDINATOR, f"{t.id} has been rejected {rounds} times", ticket=t.id)
    return rounds


class Reports:
    def __init__(self, project: Project, store: LocalStore, events: Events, git: Git, log: Callable[[str], None]):
        self.project, self.store, self.events, self.git, self.log = project, store, events, git, log

    @property
    def cfg(self):
        return self.project.config

    def apply(self, a: dict, report: dict | None, text: str, failed: bool, cause: dict | None = None) -> None:
        """What an assignment's machine reported (or, ``report`` None, that it ended without one;
        ``failed`` when it went away or its lease ran out), applied to the ticket. A report the task
        system cannot use counts as none, with what was wrong as the text."""
        why = unusable(a["role"], report)
        if why:
            self.log(f"{a['ticket']}: the {a['role']}'s report is unusable: {why}")
            report, text, failed = None, why, True
        if a["role"] == "worker":
            self._worker(a["ticket"], report, text, failed, cause)
        else:
            self._no_verdict(a["ticket"], text)

    def review_requested(self, a: dict, ev: dict) -> bool:
        """A worker passed its work on to the reviewers. Recorded (the summary as a note, the
        ticket READY_FOR_REVIEW with the commit as ``reviewed_commit``) when it can be reviewed:
        in code mode the commit named is on the branch and beyond the base. Work with nothing
        beyond the base is BLOCKED; a request naming no commit, or one not on the branch, is no
        report. True when recorded, so the review it offers stands."""
        t = self.store.get_ticket(a["ticket"])
        if t.state != State.IN_PROGRESS:
            return False
        review = ev.get("review") if isinstance(ev.get("review"), dict) else {}
        commit = review.get("commit")
        if a.get("branch"):
            # the record reads the bus behind the machines, so the branch may have moved on since
            # (the review done, the changes made): the commit must be on it, not its head
            branch = a["branch"]
            if not commit or not self.git.branch_exists(branch) or not self.git.is_ancestor(str(commit), branch):
                said = f"names commit {str(commit)[:10]}, which is not on {branch}" if commit else "names no commit"
                self.apply(a, None, f"the review request {said}", True)
                return False
            if self.git.is_ancestor(str(commit), self.cfg.base_branch):
                self._block(t, "the worker asked for a review but the branch has no commits beyond the base branch")
                return False
        self.store.add_note(t.id, Note(author="worker", text=str(review.get("summary") or "")), cause=ev)
        out = review.get("test_output")
        self.store.transition(t.id, t.version, State.READY_FOR_REVIEW, by="worker", cause=ev, feedback=None,
                              reviewed_commit=commit if a.get("branch") else t.reviewed_commit,
                              test_output=out[-8000:] if isinstance(out, str) else None)
        self.log(f"{t.id}: READY_FOR_REVIEW" + (f" at {commit[:10]}" if a.get("branch") else "") + f"; a review requested by {ev.get('source')}")
        return True

    def verdict(self, a: dict, ev: dict) -> bool:
        """A reviewer's verdict: ``changes.requested`` (rejected; the work goes back to the worker
        that did it) or ``review.approved`` (to the gate; done in tasks mode). A verdict on another
        commit than the one recorded for review is no verdict. True when it was recorded."""
        t = self.store.get_ticket(a["ticket"])
        if t.state != State.IN_REVIEW:
            return False
        commit = ev.get("commit")
        if commit and t.reviewed_commit and commit != t.reviewed_commit:
            self._no_verdict(t.id, f"the verdict names commit {commit[:10]}, not the reviewed {t.reviewed_commit[:10]}")
            return False
        verdict = "APPROVED" if ev.get("type") == APPROVED else "REJECTED"
        self.store.add_note(t.id, Note(author="reviewer", kind="review",
                                       text=f"{verdict}: {ev.get('summary') or ''}\n{ev.get('feedback') or ''}"), cause=ev)
        if verdict == "REJECTED":
            self.reject(t.id, "reviewer", ev.get("feedback") or ev.get("summary") or "", cause=ev)
            return True
        t = self.store.transition(t.id, t.version, State.APPROVED, by="reviewer", cause=ev, feedback=None, rejected_by=None)   # the gate lands reviewed_commit as recorded from the worker's request
        self.log(f"{t.id}: APPROVED by {ev.get('source')}")
        if self.cfg.work != "code":                       # nothing to merge: approved is done
            self.store.transition(t.id, t.version, State.DONE, by="reviewer", cause=ev)
            self.log(f"{t.id}: DONE")
        return True

    def stopped(self, a: dict) -> None:
        """The operator stopped the machine holding the ticket: it goes back to waiting without
        counting."""
        role = a["role"]
        self.back_to_waiting(a["ticket"], f"the {role} was stopped by the operator",
                             f"the previous worker's turn was stopped by the operator; {self.workspace_kept()}" if role == "worker" else None)

    def workspace_kept(self) -> str:
        """What the next worker finds of an interrupted turn, for its feedback."""
        if self.cfg.work == "code" and self.cfg.reuse_worktrees:
            return "worktrees are reused, so only what it committed on the branch is kept"
        return "the workspace is as it left it"

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

    def _worker(self, tid: str, report: dict | None, text: str, failed: bool, cause: dict | None) -> None:
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
                                      feedback=f"the previous worker ended without reporting; {self.workspace_kept()}")
                self.log(f"{tid}: the worker ended without reporting; back to OPEN ({crashes})")
                return
            reason = "the worker went away or ran out its lease repeatedly" if failed else "the worker finished without a report"
            reason += f". Last message: {text[-600:]}" if text else "."
            self._block(t, reason)
            return
        self.store.add_note(tid, Note(author="worker", text=report.get("summary", "")), cause=cause)
        self.store.transition(tid, t.version, State.BLOCKED, by="worker", cause=cause, blocked_reason=report["blocked_reason"])
        self.events.attention(COORDINATOR, f"{tid} BLOCKED by worker: {report['blocked_reason'][:200]}", ticket=tid)
        self.log(f"{tid}: BLOCKED by worker")

    def _block(self, t: Ticket, reason: str) -> None:
        self.store.add_note(t.id, Note(author=SCHEDULER, kind="blocker", text=reason))
        self.store.transition(t.id, t.version, State.BLOCKED, blocked_reason=reason)
        self.events.attention(COORDINATOR, f"{t.id} BLOCKED: {reason[:200]}", ticket=t.id)
        self.log(f"{t.id}: BLOCKED ({reason[:80]})")

    def _no_verdict(self, tid: str, text: str) -> None:
        """A review that ended without a verdict is redone, up to NO_VERDICT_BUDGET times in a
        row, before the ticket goes back to its worker: not a rejection, the worker has nothing to
        fix. The send-back after the budget is the task system's, never counted as a reviewer's."""
        t = self.store.get_ticket(tid)
        if t.state != State.IN_REVIEW:
            return
        tries = 1 + self._trailing(tid, "reviewer")
        if tries <= NO_VERDICT_BUDGET:
            self.store.add_note(tid, Note(author=SCHEDULER, kind="crash",
                                          text=f"the reviewer ended without a verdict (attempt {tries}){': ' + text[-300:] if text else ''}; the review will be redone"))
            self.store.transition(tid, t.version, State.READY_FOR_REVIEW)
            self.log(f"{tid}: the reviewer ended without a verdict; the review will be redone ({tries})")
            return
        self.reject(tid, SCHEDULER, f"{tries} reviews in a row ended without a verdict; the work may be too large for a review to finish")

    def reject(self, tid: str, by: str, feedback: str, counted: bool = True, cause: dict | None = None) -> None:
        """Sent back to its worker by ``by``: the reviewer (``cause``: its ``changes.requested``),
        or the task system (``task_scheduler``) when reviews keep ending without a verdict or the
        work cannot be reviewed. A send-back where nothing was reviewed (``counted`` False) is not
        a review round."""
        t = self.store.get_ticket(tid)
        rounds = send_back(self.store, self.events, self.cfg.review_rounds_before_escalation, t, by, feedback, counted, by=by,
                           cause=cause, reviewer_rejections=t.reviewer_rejections + (1 if by == "reviewer" else 0))
        self.log(f"{tid}: REJECTED by {by} (round {rounds})")
