"""External review (``[gate] mode = "external"``, code mode; SPEC.md section 5, "External
review"): the gate hands an approved branch off for a pull request instead of landing it, and the
ticket follows that pull request to resolution by what a bridge publishes about it (``github.pr.*``:
orchestra-bridges' pull-request bridge, whose event names and data fields are its contract; this
package imports nothing from it).

Opening the pull request and pushing to it are the owner's acts (or, one day, an outbox's): the
task system never pushes and never opens one. A ticket is linked to its pull request by its branch
(the pull request's head branch is the ticket's), and from then on by the pull request itself
(``owner/repo#n``), kept on the ticket as ``pull``.

What the pull request's people and checks do, applied to the ticket:

- **feedback** (changes requested, line comments, a failing check, a label the project names
  failing, conversation comments when counted) on the commit the pull request carries
  (``pull_head``), while the ticket is ``IN_EXTERNAL_REVIEW``, is gathered until none came for
  ``settle`` (a review's line comments arrive just after it), then the work goes back to the worker
  instance that did it, once: the ticket ``REJECTED`` and a ``changes.requested`` addressed ``to``
  that worker, published as whoever gave the first of the feedback (the reviewer, the pipeline's
  account that set the label, the check's app), with that event as its cause. The scheduler stands
  in for that reviewer's side of the protocol, which nobody on the pull request speaks, as it
  stands in for the coordinator's dispatch (the owner's decision D17: it publishes as the machine
  whose responsibility it implements). When that worker does not take it back within the return
  timeout, dispatch (``coordinator/dispatch``) withdraws the offer and offers the work to any
  worker, as it does any rejected work. The feedback is metadata only: who, how many comments, which
  files and lines, which label or check; the texts stay on the pull request;
- feedback on another commit than ``pull_head`` (an older head) is noted, never acted on, and so
  is feedback that comes while the worker already has the ticket back;
- an approval or a passing label is noted: only a merge closes the ticket;
- **merged**: ``DONE``, from whatever waiting state the ticket is in (whoever holds it is released
  first); **closed** without a merge: ``BLOCKED`` with the reason, for a planner; **reopened**:
  back to ``IN_EXTERNAL_REVIEW``; a **push** records the new head.

It reads the bus on from where it stopped (kept in the record with the ids it applied lately and
the feedback it is gathering), so a restart neither loses a merge nor applies one twice."""

from __future__ import annotations

import contextlib
import time
from collections import Counter, deque
from collections.abc import Callable
from pathlib import Path

from orchestra.events import machine_and_instance

from .config import Project
from .events import COORDINATOR, GATE, Events
from .git import Git, GitError
from .model import ACTIVE, TERMINAL, Note, State, Ticket
from .pool import Pool
from .reports import send_back
from .store import LocalStore

PR = "github.pr."                 # the pull-request bridge's events
HANDED_OFF = "gate.handed_off"    # the gate's: a branch handed off for its pull request
FAILED = ("failure", "timed_out", "error")     # a check's conclusions that are failures
SEEN_KEPT = 2000                  # the pull-request events lately applied, so a restart applies none twice
COMPLETE_WAIT = 3                 # a review whose line comments are not all in is waited for up to this many settle periods

# offer(ticket, source, cause, fields): the worker's turn offered back to the worker that did it,
# as ``source`` (Scheduler._offer_back); withdraw(ticket, reason): whoever holds it or is offered it
# is released and the ticket goes back to waiting (Scheduler._withdraw)
Offer = Callable[[Ticket, str, str | None, dict], bool]
Withdraw = Callable[[str, str], None]


def pull_of(ev: dict) -> str | None:
    """The pull request an event is about, ``owner/repo#n``."""
    repo, n = ev.get("repository"), ev.get("pull")
    return f"{repo}#{n}" if repo and n is not None else ev.get("subject")


def machine(source: str | None) -> str:
    return machine_and_instance(source)[0] or str(source or "unknown")


def short(sha: str | None) -> str:
    return str(sha)[:10] if sha else "?"


class External:
    def __init__(self, project: Project, store: LocalStore, events: Events, gate_store: LocalStore, gate_events: Events, git: Git, *,
                 offer: Offer, withdraw: Withdraw, log: Callable[[str], None] = print):
        self.project, self.store, self.events, self.git = project, store, events, git
        self.gate_store, self.gate_events = gate_store, gate_events      # the gate's (merge_gate): the hand-off is its
        self.offer, self.withdraw, self.log = offer, withdraw, log
        rec = store.external()
        self.offset = events.end() if rec is None else min(int(rec.get("offset", 0)), events.end())
        self.seen: deque[str] = deque((rec or {}).get("seen") or [], maxlen=SEEN_KEPT)
        self.pending: dict[str, dict] = dict((rec or {}).get("pending") or {})

    @property
    def cfg(self):
        return self.project.config.external

    def hand_off(self, t: Ticket) -> None:
        """An approved ticket handed off for its pull request, by the gate (``merge_gate``), which
        lands nothing in external review: ``IN_EXTERNAL_REVIEW``, the reviewed commit as the one the
        pull request is to carry, and ``gate.handed_off`` for the owner (or an outbox) to push and
        open it, the approval as its cause."""
        commit = t.reviewed_commit
        self.gate_store.transition(t.id, t.version, State.IN_EXTERNAL_REVIEW, because=t.caused_by, pull_head=commit)
        where = f"its pull request {t.pull}" if t.pull else "a pull request, which the owner opens"
        self.gate_store.add_note(t.id, Note(author=GATE, kind="gate", text=f"handed off {t.branch} at {short(commit)} for {where}; "
                                            "in external review the gate lands nothing: the ticket is done when the pull request is merged"))
        self.gate_events.emit(HANDED_OFF, cause=t.caused_by, ticket=t.id, branch=t.branch, commit=commit,
                              base=self.project.config.base_branch, repository=self.cfg.repository, pull=t.pull)
        self.log(f"{t.id}: IN_EXTERNAL_REVIEW: {t.branch} at {short(commit)} handed off for {where}")

    def follow(self) -> None:
        """What the bridge published since the last call, applied in the bus's order; then the
        feedback that has settled re-engages its worker."""
        evs, end = self.events.after(self.offset)
        acted = False
        for ev in evs:
            if not str(ev.get("type") or "").startswith(PR) or ev.get("id") in self.seen:
                continue
            try:
                self._apply(ev)
            except Exception as e:  # noqa: BLE001 - one bad event never loses the others
                self.log(f"{ev.get('subject')}: cannot apply {ev.get('type')} from {ev.get('source')}: {e!r}")
            self.seen.append(ev.get("id"))
            acted = True
        self.offset = end
        now_t = time.time()
        for tid in [tid for tid, p in self.pending.items() if self._due(p, now_t)]:
            p = self.pending.pop(tid)
            try:
                self._engage(tid, p)
            except Exception as e:  # noqa: BLE001
                self.log(f"{tid}: cannot re-engage its worker after feedback on {p.get('pull')}: {e!r}")
            acted = True
        if acted:
            self.store.set_external(self.offset, list(self.seen), self.pending)

    def _apply(self, ev: dict) -> None:
        if self.cfg.repository and ev.get("repository") != self.cfg.repository:
            return
        kind, pull = ev["type"][len(PR):], pull_of(ev)
        t = self._ticket(ev, kind, pull)
        if t is None:
            return
        who = ev.get("source") or "someone"
        if kind == "merged":
            self._merged(t, ev, pull, who)
        elif kind == "closed":
            self._closed(t, ev, pull, who)
        elif kind == "reopened":
            self._reopened(t, ev, pull, who)
        elif kind == "pushed":
            self._pushed(t, ev, pull, who)
        elif kind == "opened":
            self._note(t, who, f"{who} opened {pull} at {short(ev.get('head'))}")
        elif self._failing(kind, ev, t.id):
            self._feedback(t, ev, kind, pull, who)
        elif kind == "review_submitted" and ev.get("state") == "approved":
            self._note(t, who, f"{who} approved {pull} at {short(ev.get('head'))}; only a merge closes the ticket")
        elif kind == "labelled" and ev.get("label") in self.cfg.passing_labels:
            self._note(t, who, f"{who} added the passing label {ev.get('label')} to {pull} at {short(ev.get('head'))}; "
                       "only a merge closes the ticket")

    def _ticket(self, ev: dict, kind: str, pull: str | None) -> Ticket | None:
        """The ticket an event is about: the one linked to its pull request; else the one whose
        branch is the pull request's, not finished, which is linked to it now (to a new pull request
        only when that one is opened: a ticket follows one pull request at a time)."""
        tickets = self.store.list_tickets()
        t = next((t for t in tickets if t.pull == pull), None)
        branch = ev.get("branch")
        if t is not None or not branch:
            return t
        t = next((t for t in tickets if t.branch == branch and t.state not in TERMINAL and (t.pull is None or kind == "opened")), None)
        if t is None:
            return None
        was = t.pull
        t = self.store.update_ticket(t.id, pull=pull, pull_state="open", pull_head=t.pull_head or ev.get("head"))
        self._note(t, ev.get("source") or "someone", f"linked to the pull request {pull} by its branch {branch}"
                   + (f", in place of {was}" if was else ""))
        self.log(f"{t.id}: linked to {pull}")
        return t

    def _failing(self, kind: str, ev: dict, tid: str) -> bool:
        """Whether an event is feedback that sends the work back (``[gate.external]`` says which)."""
        c = self.cfg
        if kind == "review_submitted":
            return ev.get("state") == "changes_requested" or (c.line_comments and ev.get("state") == "commented" and (ev.get("comments") or 0) > 0)
        if kind == "review_commented":                # a comment of a review gathered already belongs to it, counted or not
            return c.line_comments or any(r.get("review") == ev.get("review") for r in (self.pending.get(tid) or {}).get("reviews", []))
        if kind == "commented":
            return c.conversation_comments
        if kind == "labelled":
            return ev.get("label") in c.failing_labels
        if kind == "check_completed":
            return c.failing_checks and ev.get("conclusion") in FAILED
        return False

    def _feedback(self, t: Ticket, ev: dict, kind: str, pull: str | None, who: str) -> None:
        """Feedback that sends the work back, gathered for the ticket while it is in external review
        and about the commit its pull request carries; otherwise noted."""
        what = describe_one(kind, ev, who)
        head = ev.get("head")
        if t.state != State.IN_EXTERNAL_REVIEW:
            self._note(t, who, f"{what} on {pull} while the ticket is {t.state}: noted, not acted on")
            return
        if t.pull_head and head and head != t.pull_head:
            self._note(t, who, f"{what} on {pull} is about {short(head)}, not its head {short(t.pull_head)}: stale, noted, not acted on")
            self.log(f"{t.id}: stale feedback on {pull} ({kind} on {short(head)}, head {short(t.pull_head)}) noted")
            return
        now_t = time.time()
        p = self.pending.setdefault(t.id, {"pull": pull, "head": head or t.pull_head, "source": who, "cause": ev.get("id"), "first": now_t,
                                           "reviews": [], "comments": [], "conversation": [], "labels": [], "checks": []})
        p["last"] = now_t
        if kind == "review_submitted":
            p["reviews"].append({"by": who, "state": ev.get("state"), "comments": ev.get("comments") or 0, "review": ev.get("review")})
        elif kind == "review_commented":
            p["comments"].append({"by": who, "path": ev.get("path"), "line": ev.get("line"), "review": ev.get("review")})
        elif kind == "commented":
            p["conversation"].append({"by": who})
        elif kind == "labelled":
            p["labels"].append({"by": who, "label": ev.get("label")})
        else:
            p["checks"].append({"by": who, "check": ev.get("check"), "conclusion": ev.get("conclusion")})

    def _due(self, p: dict, now_t: float) -> bool:
        """Feedback re-engages once none came for ``settle``, and a review's line comments are all
        in (or it waited ``COMPLETE_WAIT`` settle periods for them)."""
        settle = self.cfg.settle
        if now_t - p.get("last", p["first"]) < settle:
            return False
        got = Counter(c.get("review") for c in p["comments"])
        complete = all(got[r.get("review")] >= r["comments"] for r in p["reviews"])
        return complete or now_t - p["first"] >= COMPLETE_WAIT * max(settle, 1.0)

    def _engage(self, tid: str, p: dict) -> None:
        """The gathered feedback sends the work back to the worker that did it: ``REJECTED`` (a
        review round, counted ``by`` whoever gave it), and its turn offered back ``to`` that worker
        as ``changes.requested`` from whoever gave the first of it (``offer``)."""
        t = self.store.get_ticket(tid)
        if t.state != State.IN_EXTERNAL_REVIEW or (t.pull_head and p["head"] and p["head"] != t.pull_head):
            self.log(f"{tid}: feedback on {p['pull']} gathered, but the ticket is {t.state} at {short(t.pull_head)} now: noted only")
            return
        by = machine(p["source"])
        summary, feedback, meta = describe(p)
        self.store.add_note(tid, Note(author=by, kind="review", text=f"REJECTED on the pull request: {summary}"))
        send_back(self.store, self.events, self.project.config.review_rounds_before_escalation, t, by, feedback, by=by,
                  cause={"id": p["cause"]}, ask_as=GATE, pull_rejections=t.pull_rejections + 1)
        t = self.store.get_ticket(tid)
        offered = bool(t.worker) and self.offer(t, p["source"], p["cause"],
                                                {"feedback": feedback, "summary": summary, "commit": p["head"], "pull": p["pull"], "pull_feedback": meta})
        self.log(f"{tid}: REJECTED by feedback on {p['pull']} ({summary}); " + (f"back to {t.worker}" if offered else "back in the queue"))

    def _merged(self, t: Ticket, ev: dict, pull: str | None, who: str) -> None:
        """Merged on the other side: the ticket is done, whatever it waited for; whoever holds it
        or is offered it is released first, and its worktree and branch go."""
        if t.state in TERMINAL:
            return
        self.pending.pop(t.id, None)
        self.withdraw(t.id, f"its pull request {pull} was merged by {who}")
        t = self.store.get_ticket(t.id)
        if t.state in ACTIVE:
            self.log(f"{t.id}: {pull} merged, but the ticket is still {t.state}; left as it is")
            return
        self._note(t, who, f"{who} merged {pull} at {short(ev.get('head'))}" + (f" as {short(ev.get('merge_commit'))}" if ev.get("merge_commit") else ""))
        self.store.transition(t.id, t.version, State.DONE, by=machine(who), cause=ev, pull_state="merged", pull_head=ev.get("head") or t.pull_head)
        self._drop(t)
        self.log(f"{t.id}: DONE: {pull} merged by {who}")

    def _closed(self, t: Ticket, ev: dict, pull: str | None, who: str) -> None:
        """Closed without a merge: a ticket in external review is ``BLOCKED`` with the reason, for a
        planner (reopen it, unblock it to have the work redone for a new pull request, or cancel);
        the merge gate, whose hand-off it was, asks the coordinator."""
        if t.state == State.IN_EXTERNAL_REVIEW:
            self.pending.pop(t.id, None)
            reason = (f"its pull request {pull} was closed without a merge by {who}: reopen it, or unblock the ticket to have the "
                      "work redone for a new pull request, or cancel it")
            self._note(t, who, f"{who} closed {pull} without a merge")
            t = self.store.get_ticket(t.id)
            self.store.transition(t.id, t.version, State.BLOCKED, by=machine(who), cause=ev, blocked_reason=reason, pull_state="closed")
            self.gate_events.attention(COORDINATOR, f"{t.id} BLOCKED: {reason[:200]}", ticket=t.id)
            self.log(f"{t.id}: BLOCKED: {pull} closed by {who}")
        elif t.state not in TERMINAL:
            self.store.update_ticket(t.id, pull_state="closed")
            self._note(t, who, f"{who} closed {pull} without a merge while the ticket is {t.state}: noted")

    def _reopened(self, t: Ticket, ev: dict, pull: str | None, who: str) -> None:
        if t.state == State.BLOCKED and t.pull_state == "closed":
            self._note(t, who, f"{who} reopened {pull}")
            t = self.store.get_ticket(t.id)
            self.store.transition(t.id, t.version, State.IN_EXTERNAL_REVIEW, by=machine(who), cause=ev, blocked_reason=None, pull_state="open")
            self.log(f"{t.id}: IN_EXTERNAL_REVIEW again: {pull} reopened by {who}")
        elif t.state not in TERMINAL:
            self.store.update_ticket(t.id, pull_state="open")
            self._note(t, who, f"{who} reopened {pull}")

    def _pushed(self, t: Ticket, ev: dict, pull: str | None, who: str) -> None:
        """A new head: feedback is about it from now on (unless it is older than the head known)."""
        head = ev.get("head")
        if not head or t.state in TERMINAL or head == t.pull_head:
            return
        if t.pull_head and self.git.is_ancestor(head, t.pull_head):
            return
        self.store.update_ticket(t.id, pull_head=head)
        self._note(t, who, f"{who} pushed {short(head)} to {pull}")

    def _note(self, t: Ticket, who: str, text: str) -> None:
        self.store.add_note(t.id, Note(author=machine(who), kind="pull", text=text))

    def _drop(self, t: Ticket) -> None:
        """A merged ticket's worktree and branch, as a landing removes them: the work is upstream."""
        wt = self.store.worktree(t.id)
        if wt:
            with contextlib.suppress(GitError):
                self.git.worktree_remove(Path(wt["path"]))
            self.store.remove_worktree(t.id)
        if t.branch:
            with contextlib.suppress(GitError):
                if self.project.config.reuse_worktrees:
                    Pool(self.project, self.store, self.git, self.log).release_branch(t.branch)
                self.git.branch_delete(t.branch)


def describe_one(kind: str, ev: dict, who: str) -> str:
    if kind == "review_submitted":
        n = ev.get("comments") or 0
        return f"{who} {'requested changes' if ev.get('state') == 'changes_requested' else 'commented'}" + (f" with {n} line comment(s)" if n else "")
    if kind == "review_commented":
        return f"{who} commented on {ev.get('path')}:{ev.get('line')}"
    if kind == "commented":
        return f"{who} commented on the conversation"
    if kind == "labelled":
        return f"{who} added the label {ev.get('label')}"
    return f"the check {ev.get('check')} ended {ev.get('conclusion')}"


def describe(p: dict) -> tuple[str, str, dict]:
    """Gathered feedback as the worker is told it: a summary, the feedback (what to do, and where
    the texts are), and the metadata (``pull_feedback``). Never a text from the pull request: the
    bus carries none."""
    parts = []
    for r in p["reviews"]:
        n = r["comments"]
        parts.append(f"{r['by']} {'requested changes' if r['state'] == 'changes_requested' else 'commented'}"
                     + (f" with {n} line comment(s)" if n else ""))
    if p["comments"]:
        at = ", ".join(f"{c['path']}:{c['line']}" for c in p["comments"])
        parts.append(f"line comments at {at}")
    if p["conversation"]:
        parts.append(f"{len(p['conversation'])} comment(s) on the conversation by {', '.join(sorted({c['by'] for c in p['conversation']}))}")
    parts += [f"the label {lb['label']} added by {lb['by']}" for lb in p["labels"]]
    parts += [f"the check {c['check']} ended {c['conclusion']}" for c in p["checks"]]
    summary = f"{p['pull']} at {short(p['head'])}: " + "; ".join(parts)
    feedback = (f"Feedback on the pull request {p['pull']}, at {short(p['head'])}: " + "; ".join(parts) + ". The texts are on the pull "
                "request, not here: read them there (the review comments at the files and lines named, the label's or check's run), "
                "address them on this branch, commit, and pass the work on for review as before.")
    meta = {k: p[k] for k in ("pull", "head", "reviews", "comments", "labels", "checks")} | {"conversation": len(p["conversation"])}
    return summary, feedback, meta
