"""Stacked branches (``[gate] stacking = "git-spice"``, code mode; SPEC.md section 5, "Stacked
branches"; goal G7 in Orchestra's ``docs/roadmap/goals.md``). A ticket that depends on work not
landed yet branches from that work's branch instead of waiting for it to land, and is kept rebased
on it; several tickets on one form a tree. git-spice records the tree (tasking/spice.py); the
ticket keeps its parent as ``stacked_on``.

The policy:

- **Stacking.** A ticket waiting in ``OPEN`` whose dependencies are all ``DONE`` but one is ready as
  before, on a branch from the base. One that has exactly one dependency not landed is ready too,
  on a branch from that dependency's, once that work stands for review (``READY_FOR_REVIEW``,
  ``IN_REVIEW``, ``APPROVED`` or ``IN_EXTERNAL_REVIEW``: it has a reviewed commit and nobody is
  changing it; not ``MERGING``, which is about to land) and the stack below it holds fewer than ``stack_depth`` unlanded branches.
  Two or more dependencies not landed: it waits, since a branch has one parent.
- **Restacking.** Each tick, a stacked branch whose parent's head is not in it is rebased onto it
  (``git-spice branch restack``), unless the ticket is held or offered (a machine works in it), or
  its parent is being worked (``IN_PROGRESS``: its head is moving). Parents first, so a change
  climbs the tree in one tick. The reviewed commit moves with the rebase. A clean restack is not a
  new review: the change is the same patch on a new base, and it is tested again where it lands
  (the gate's stages, or the pull request's checks).
- **Conflicts** go back to the ticket's worker as rework, by the merge gate (not a review round):
  from ``READY_FOR_REVIEW``, ``APPROVED`` or ``IN_EXTERNAL_REVIEW`` the ticket is ``REJECTED`` with
  the conflicted files in its feedback and ``stack.conflict``; waiting for a worker already, the
  feedback says so; ``BLOCKED``, a note. The branch is left as it was. The same conflict (the same
  two heads) is not tried again.
- **Landing.** In ``land`` mode the gate takes a stacked ticket only once its parent has landed and
  its branch has been moved onto the base: the bottom of a stack lands first. Once the parent is
  ``DONE`` (its branch deleted by the landing; git-spice moves the branches on it onto the base),
  the child is restacked onto the base, ``stacked_on`` cleared, and the gate tests it again.
- **External review.** The gate hands a stacked ticket off with its parent's branch as the pull
  request's base (``gate.handed_off`` {base, parent}); pushing and opening stay the owner's
  (``git-spice stack submit`` does it with the bases right; ``orchestra tasks stack`` lists them). When the
  parent's pull request is merged (``github.pr.merged`` with ``merge_commit``, kept as the parent's
  ``merged_as``), the child is restacked onto the base if the base has that commit, or onto the
  commit itself (fetched from the remote if it is not here) when the base does not have it yet:
  the squash commit replaces the parent's commits. Each restack of a branch in external review is
  ``stack.restacked`` {pull}: the owner pushes it.

Every restack and conflict is the merge gate's act (``merge_gate``; the owner's decision D17:
keeping branches mergeable is the gate's responsibility), counted as ``tasks_restacks_total``."""

from __future__ import annotations

import contextlib
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .config import Project
from .events import COORDINATOR, GATE, Events
from .git import Git, GitError
from .model import ACTIVE, TERMINAL, MilestoneStatus, Note, State, Ticket, is_ready
from .pool import Pool
from .reports import send_back
from .spice import Restacked, Spice
from .store import LocalStore

RESTACKED, CONFLICT = "stack.restacked", "stack.conflict"
FETCH_EVERY_S = 60.0          # a squash commit not here is fetched for at most this often
STANDS = {State.READY_FOR_REVIEW, State.IN_REVIEW, State.APPROVED, State.IN_EXTERNAL_REVIEW}   # work a child may stack on (not MERGING: it is landing)
SENT_BACK = {State.READY_FOR_REVIEW, State.APPROVED, State.IN_EXTERNAL_REVIEW}    # a conflict in these rejects the ticket


def short(sha: str | None) -> str:
    return str(sha)[:10] if sha else "?"


class Stacks:
    def __init__(self, project: Project, store: LocalStore, git: Git, gate_store: LocalStore, gate_events: Events,
                 log: Callable[[str], None] = print):
        self.project, self.store, self.git, self.log = project, store, git, log
        self.gate_store, self.gate_events = gate_store, gate_events        # the merge gate's: restacks are its acts
        self.conflicted: dict[str, tuple[str, str]] = {}                   # ticket -> (its head, the head it conflicted with)
        self.said: set[str] = set()                                         # what the log said once
        self.fetched: dict[str, float] = {}                                 # a squash commit not here -> when it was last fetched for
        self.inited = False

    @property
    def cfg(self):
        return self.project.config

    @property
    def on(self) -> bool:
        return self.cfg.stacked

    @property
    def spice(self) -> Spice:
        return Spice(self.project.repo_root, self.cfg.base_branch)

    def _once(self, key: str, msg: str) -> None:
        if key not in self.said:
            self.said.add(key)
            self.log(msg)

    # what may start, and on what

    def parent(self, t: Ticket, tickets: dict[str, Ticket]) -> Ticket | None:
        """The unlanded ticket ``t``'s branch stacks on: its recorded parent once its branch is made,
        else the one dependency not ``DONE`` when that work stands for review and the stack is not
        too deep; None when it stacks on nothing (or must wait)."""
        if t.stacked_on:
            return tickets.get(t.stacked_on)
        if any(d not in tickets for d in t.depends_on):
            return None
        below = [tickets[d] for d in t.depends_on if tickets[d].state != State.DONE]
        if len(below) != 1:
            return None
        p = below[0]
        if p.state not in STANDS or not p.reviewed_commit or not p.branch or not self.git.branch_exists(p.branch):
            return None
        return p if self.depth(p, tickets) < self.cfg.stack_depth else None

    def depth(self, t: Ticket, tickets: dict[str, Ticket]) -> int:
        """How many unlanded branches ``t``'s is, counting itself and those below it."""
        n, seen = 0, set()
        while t is not None and t.state != State.DONE and t.id not in seen:
            seen.add(t.id)
            n += 1
            t = tickets.get(t.stacked_on) if t.stacked_on else None
        return n

    def ready(self, t: Ticket, tickets: dict, milestones: dict) -> bool:
        """Ready as without stacking, or ready to stack on the one dependency not landed."""
        if is_ready(t, tickets, milestones):
            return True
        ms = milestones.get(t.milestone_id)
        if not self.on or t.state != State.OPEN or ms is None or ms.status != MilestoneStatus.OPEN:
            return False
        return self.parent(t, tickets) is not None

    def base(self, t: Ticket) -> str:
        """The branch ``t``'s branch is based on: its parent's while the parent is not landed, else
        the base branch. Offers name it as ``base`` (what the work is beyond), and so does the
        hand-off for its pull request."""
        if self.on and t.stacked_on:
            with contextlib.suppress(Exception):
                p = self.store.get_ticket(t.stacked_on)
                if p.state != State.DONE and p.branch:
                    return p.branch
        return self.cfg.base_branch

    def may_land(self, t: Ticket) -> bool:
        """The gate lands a ticket only when its branch is on the base: never above an unlanded parent."""
        return not (self.on and t.stacked_on)

    def prepare(self, t: Ticket, branch: str) -> None:
        """Before a worker's worktree is made: the ticket's branch, when new, from its parent's head
        (recorded as ``stacked_on``) or from the base, tracked by git-spice either way."""
        if not self.on:
            return
        self._init()
        tree = self.spice.tree()
        if self.git.branch_exists(branch):
            if branch not in tree:
                self.spice.track(branch, self.base(t))
            return
        tickets = self.store.tickets_by_id()
        p = self.parent(t, tickets)
        if p is None and not is_ready(t, tickets, self.store.milestones_by_id()):
            # its dependency moved on since the queue looked (the gate took it): a branch from the base now would miss it
            raise GitError(f"{t.id} waits: its dependency is neither landed nor standing for review now")
        base = p.branch if p else self.cfg.base_branch
        self.git.run("branch", branch, base)
        self.spice.track(branch, base)
        if p:
            self.gate_store.update_ticket(t.id, stacked_on=p.id, branch=branch)
            self.gate_store.add_note(t.id, Note(author=GATE, kind="gate", text=f"{branch} branches from {p.branch} ({p.id}, not landed yet) "
                                                f"at {short(self.git.rev_parse(p.branch))}; it lands after {p.id}"))
            self.log(f"{t.id}: stacked on {p.id} ({p.branch})")

    def _init(self) -> None:
        if not self.inited:
            if not self.spice.available():
                raise GitError('[gate] stacking = "git-spice", but git-spice is not on PATH')
            self.spice.ensure()
            self.inited = True

    # keeping the tree restacked

    def tick(self) -> None:
        """Every stacked branch that is not on its parent's head (or, the parent landed, on the
        base) restacked, parents first; see the module's policy."""
        if not self.on:
            return
        tickets = self.store.tickets_by_id()
        stacked = [t for t in tickets.values() if t.stacked_on and t.state not in TERMINAL and t.branch]
        if not stacked:
            return
        try:
            self._init()
            self.spice.tree()                     # git-spice moves branches off any branch deleted out of band (a landing's)
        except GitError as e:
            self._once("spice", f"stacking: {e}")
            return
        held = {a["ticket"] for a in self.store.assignments().values()}
        for t in sorted(stacked, key=lambda t: self.depth(t, tickets)):
            t = self.store.get_ticket(t.id)          # a restack below may have changed it
            p = tickets.get(t.stacked_on or "")
            if p is None or t.id in held or t.state in ACTIVE or not self.git.branch_exists(t.branch):
                continue
            p = self.store.get_ticket(p.id)
            try:
                self._follow(t, p)
            except GitError as e:
                self.log(f"{t.id}: cannot restack {t.branch}: {e}")

    def _follow(self, t: Ticket, p: Ticket) -> None:
        if p.state == State.CANCELLED:
            key = f"cancelled:{t.id}:{p.id}"
            if key not in self.said:
                self.said.add(key)
                why = f"{t.id}'s branch {t.branch} is stacked on {p.id}, which was cancelled: it carries {p.id}'s commits and cannot land"
                self.gate_store.add_note(t.id, Note(author=GATE, kind="gate", text=why))
                self.gate_events.attention(COORDINATOR, why + "; cancel it, or have it redone from the base", ticket=t.id)
            return
        if p.state == State.DONE:
            self._onto_base(t, p)
        elif p.state != State.IN_PROGRESS and p.branch and self.git.branch_exists(p.branch) \
                and not self.git.is_ancestor(p.branch, t.branch):
            self._restack(t, p.branch, self.git.rev_parse(p.branch))

    def _onto_base(self, t: Ticket, p: Ticket) -> None:
        """The parent landed: the branch moved onto the base (its parent's commits dropped: they are
        in the base, merged or squashed), ``stacked_on`` cleared."""
        base = self.cfg.base_branch
        if p.branch and self.git.branch_exists(p.branch):        # a landing deletes it; a merge elsewhere may not have
            with contextlib.suppress(GitError):
                Pool(self.project, self.store, self.git, self.log).release_branch(p.branch)
            self.git.branch_delete(p.branch)
            self.spice.tree()
        squash = p.merged_as
        if squash and not self.git.is_ancestor(squash, base) and not self.git.is_ancestor(squash, t.branch):
            if not self._have(squash):
                self._once(f"missing:{squash}", f"{t.id}: {p.id} was merged as {short(squash)}, which is not here (fetched from no remote "
                           f"that has it); {t.branch} waits for it")
                return
            self._restack(t, short(squash), squash, squash=squash, below=p.pull_head or p.reviewed_commit)
            return
        self._restack(t, base, self.git.rev_parse(base), landed=True)

    def _have(self, commit: str) -> bool:
        """Whether ``commit`` is here, fetched from the repository's remotes if not (at most once a
        ``FETCH_EVERY_S``): fetching moves remote-tracking refs only, never a branch."""
        has = lambda: self.git.run("cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode == 0
        if has():
            return True
        if time.time() - self.fetched.get(commit, 0.0) < FETCH_EVERY_S:
            return False
        self.fetched[commit] = time.time()
        for remote in self.git.run("remote", check=False).stdout.split():
            self.git.run("fetch", "--quiet", remote, check=False, timeout=120)
            if has():
                return True
        return False

    def _restack(self, t: Ticket, onto: str, onto_head: str, *, squash: str | None = None, below: str | None = None,
                 landed: bool = False) -> None:
        old, parent = self.git.rev_parse(t.branch), t.stacked_on
        if self.conflicted.get(t.id) == (old, onto_head):
            return
        where = self._where(t)
        if where is None:
            return
        try:
            if squash:
                res = self._rebase_onto(where, t.branch, squash, self.git.run("merge-base", t.branch, below or squash).stdout.strip())
                if res.ok:
                    self.spice.track(t.branch, self.cfg.base_branch)
            else:
                res = self.spice.restack(t.branch, where)
        finally:
            if where == self.project.stack_dir:
                self.git.run("checkout", "-q", "--detach", cwd=where, check=False)
        if not res.ok:
            self.conflicted[t.id] = (old, onto_head)
            self._conflict(t, onto, res.conflict)
            return
        self.conflicted.pop(t.id, None)
        new = self.git.rev_parse(t.branch)
        moved = {"stacked_on": None} if (landed or squash) else {}
        if new == old and not moved:
            return
        moved["reviewed_commit"] = self._moved(t.reviewed_commit, old, new)
        t = self.gate_store.update_ticket(t.id, **moved)
        what = f"restacked {t.branch} onto {onto} at {short(onto_head)}: {short(old)} -> {short(new)}"
        if t.state == State.IN_EXTERNAL_REVIEW:
            what += "; the owner pushes it to its pull request" + (f" {t.pull}" if t.pull else "") + (f", now based on {onto}" if "stacked_on" in moved else "")
        self.gate_store.add_note(t.id, Note(author=GATE, kind="gate", text=what))
        self.gate_events.emit(RESTACKED, ticket=t.id, branch=t.branch, onto=onto, onto_commit=onto_head, previous=old, commit=new,
                              parent=parent, pull=t.pull)
        self.gate_store.count("restack", outcome="restacked", by=GATE)
        self.log(f"{t.id}: {what}")

    def _moved(self, reviewed: str | None, old: str, new: str) -> str | None:
        """The reviewed commit after a rebase: as many commits below the new head as it was below the
        old one (a rebase keeps the branch's own commits in order)."""
        if not reviewed or not self.git.is_ancestor(reviewed, old):
            return reviewed
        n = int(self.git.run("rev-list", "--count", f"{reviewed}..{old}").stdout.strip() or 0)
        return self.git.rev_parse(f"{new}~{n}") if n else new

    def _where(self, t: Ticket) -> Path | None:
        """The worktree to restack in: the ticket's own when it has one on its branch, else the stack
        worktree with the branch released from any idle worktree of the pool. None (try later) when
        a worktree in use has it, or the one to use has changes not committed."""
        wt = self.store.worktree(t.id)
        if wt and Path(wt["path"]).is_dir() and self.git.worktree_branch(Path(wt["path"])) == t.branch:
            where = Path(wt["path"])
        else:
            try:
                Pool(self.project, self.store, self.git, self.log).release_branch(t.branch)
            except GitError as e:
                self._once(f"busy:{t.id}", f"{t.id}: not restacked yet: {e}")
                return None
            where = self.project.stack_dir
            if not (where / ".git").exists():
                self.git.run("worktree", "prune", check=False)
                self.git.run("worktree", "add", "--detach", str(where), self.cfg.base_branch)
            for op in ("rebase", "merge"):
                self.git.run(op, "--abort", cwd=where, check=False)
            self.git.run("reset", "-q", "--hard", cwd=where)
        if self.git.run("status", "--porcelain", "--untracked-files=no", cwd=where).stdout.strip():
            self._once(f"dirty:{t.id}:{where}", f"{t.id}: not restacked yet: {where} has changes not committed")
            return None
        return where

    def _rebase_onto(self, where: Path, branch: str, onto: str, upstream: str) -> Restacked:
        """``branch``'s own commits (those after ``upstream``) onto ``onto``, by git itself: a squash
        commit the base does not have yet is no branch git-spice can restack onto."""
        p = subprocess.run(["git", "-C", str(where), "rebase", "--onto", onto, upstream, branch], capture_output=True, text=True, check=False)
        if p.returncode == 0:
            return Restacked(True, [], p.stdout + p.stderr)
        files = self.git.run("diff", "--name-only", "--diff-filter=U", cwd=where, check=False).stdout.split()
        self.git.run("rebase", "--abort", cwd=where, check=False)
        if not files:
            raise GitError(f"git rebase --onto {short(onto)} {short(upstream)} {branch}: {(p.stderr or p.stdout).strip()[-1500:]}")
        return Restacked(False, files, p.stdout + p.stderr)

    def _conflict(self, t: Ticket, onto: str, files: list[str]) -> None:
        """A restack that conflicts: the work goes back to its worker (see the module's policy)."""
        listed = ", ".join(files)
        feedback = (f"Restacking {t.branch} onto {onto} conflicts in {listed}: what this branch builds on changed under it. Rebase "
                    f"the branch onto {onto} (`git rebase {onto}`), resolve the conflicts in those files, commit, and pass the work on "
                    "for review again. Nothing was changed on the branch.")
        self.gate_store.add_note(t.id, Note(author=GATE, kind="gate", text=f"restacking {t.branch} onto {onto} conflicts in {listed}"))
        t = self.store.get_ticket(t.id)
        if t.state in SENT_BACK:
            send_back(self.gate_store, self.gate_events, self.cfg.review_rounds_before_escalation, t, GATE, feedback, counted=False,
                      by=GATE, gate_rejections=t.gate_rejections + 1)
            outcome = "sent back to its worker"
        elif t.state in (State.OPEN, State.REJECTED):
            self.gate_store.update_ticket(t.id, feedback=((t.feedback + "\n\n") if t.feedback else "") + feedback)
            outcome = "its worker is told when it takes it"
        else:
            outcome = "noted"
        self.gate_events.emit(CONFLICT, ticket=t.id, branch=t.branch, onto=onto, files=files, parent=t.stacked_on)
        self.gate_store.count("restack", outcome="conflict", by=GATE)
        self.log(f"{t.id}: restacking {t.branch} onto {onto} conflicts in {listed}; {outcome}")


def listing(project: Project, store: LocalStore) -> list[dict]:
    """The tickets' branches as a tree, for whoever submits the stack (the owner, ``git-spice stack
    submit``, an outbox one day): each unfinished ticket with a branch, the branch its pull request
    is to be based on (its parent's, or the base), whether the branch is on its base's head now, its
    head, and its pull request. Read from the record and git only: nothing is changed."""
    git, base_branch = Git(project.repo_root), project.config.base_branch
    tickets = store.tickets_by_id()
    out = []
    for t in sorted(tickets.values(), key=lambda t: t.id):
        if t.state in TERMINAL or not t.branch or not git.branch_exists(t.branch):
            continue
        p = tickets.get(t.stacked_on or "")
        base = p.branch if p is not None and p.state != State.DONE and p.branch else base_branch
        on = git.branch_exists(base) and git.is_ancestor(base, t.branch)
        out.append({"ticket": t.id, "state": t.state.value, "branch": t.branch, "base": base, "parent": t.stacked_on,
                    "head": git.rev_parse(t.branch), "on_base": on, "pull": t.pull})
    return out
