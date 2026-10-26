"""The task system's scheduler: it offers ready tickets to the roles that work them (a worker, a
reviewer) over Orchestra's bus, applies what the machines that took them report, lands approved
code through the merge gate, and runs the probes in ``orchestra.toml``. It runs no agent: whatever
fills a role answers (orchestra-agents' AI agents, a script; tasking/assignments.py). SPEC.md
section 6."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .assignments import (
    ACCEPTED,
    ASSIGNED,
    LEASE_S,
    RELEASED,
    REPORTED,
    WORKING,
    Follower,
    brief,
    new_id,
)
from .config import Project
from .events import Events
from .gate import run_gate
from .gate.runner import run_specs
from .git import Git, GitError
from .model import ACTIVE, RECOVERY, Note, State, Ticket, effective_priorities, is_ready
from .probes import Prober, config_probes
from .staging import Staging
from .store import Conflict, LocalStore, StoreError

HOST_LOG: Path | None = None          # <state>/logs/orchestra-host.log once a loop starts
HOST_LOG_MAX = 5_000_000              # bytes; the file is rotated to .1 past this


def log(msg: str) -> None:
    line = f"[orchestra {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if HOST_LOG is not None:            # the bootstrap machine's log, shown on the Core page
        try:
            if HOST_LOG.exists() and HOST_LOG.stat().st_size > HOST_LOG_MAX:
                HOST_LOG.replace(HOST_LOG.with_suffix(".log.1"))
            with HOST_LOG.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


class Loop:
    def __init__(self, project: Project, store: LocalStore, events: Events):
        global HOST_LOG
        HOST_LOG = project.logs_dir / "orchestra-host.log"
        HOST_LOG.parent.mkdir(parents=True, exist_ok=True)
        self.project = project
        self.store = store
        self.events = events
        self.git = Git(project.repo_root)
        self.cfg = project.config
        self.gate_thread: threading.Thread | None = None
        self.preparing: dict[str, threading.Thread] = {}   # assignment -> the review being prepared (its tests run)
        self.stop = threading.Event()
        self.staging = Staging(project, store, events)
        self.prober = Prober(project, store, events)
        self.killed: set[str] = set()                # ticket ids whose assignee the operator stopped
        self.follow = Follower(events)               # what the assignees say back (tasking/assignments.py)

    # -- lifecycle -------------------------------------------------------------

    def recover(self) -> None:
        for aid in self.store.assignments():
            self.store.drop_assignment(aid)
        for t in self.store.list_tickets():
            if t.state in ACTIVE:
                self.store.transition(t.id, t.version, RECOVERY[t.state])
                log(f"recovered {t.id}: {t.state} -> {RECOVERY[t.state]}")
        # A gate run that was in flight when the previous loop died never finished;
        # mark it so the dashboard does not show it as running forever.
        for run in self.store.gate_runs():
            if run.get("status") == "running":
                run["status"] = "aborted"
                run["summary"] = "orchestrator restarted during the run"
                run["finished_at"] = run.get("finished_at") or __import__("tasking.model", fromlist=["now"]).now()
                self.store.save_gate_run(run)
        for p in self.project.gate_dir.glob("*"):
            self.git.worktree_remove(p)
        self.git.run("worktree", "prune", check=False)
        for b in self.git.run("branch", "--list", "orchestra/gate-*", check=False).stdout.split():
            self.git.branch_delete(b.strip("* "))

    def run(self, once: bool = False) -> None:
        import signal

        def _stop(_sig, _frame):
            self.stop.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _stop)
            except ValueError:  # not the main thread (tests)
                pass
        self.recover()
        me = {"pid": os.getpid(), "started_at": time.time()}
        self.store._mutate_runtime(lambda d: d.__setitem__("scheduler", me))
        from .requests import REQUESTS, Desk
        self.desk = Desk(self.project, self.store, self.events, log)   # planners' requests (tasking/requests.py)
        self.desk.start()
        self.events.emit("loop_started")
        self.events.publish("orchestra.machine.started", {"package": "tasks", "mode": "continuous", "kind": "program",
                                                          "listens_for": sorted({*REQUESTS.values(), ACCEPTED, WORKING, REPORTED}),
                                                          "does": "offers ready tickets to workers and reviewers and applies their reports"})
        log(f"project {self.project.key} on {self.project.repo_root}; workers={self.cfg.workers} reviewers={self.cfg.reviewers}")
        paused = self.store.pipeline()
        if paused.get("paused"):
            self.events.attention("supervisor", f"the pipeline is still paused after a restart: {paused.get('reason')}. "
                                  "Diagnose, fix what you can or escalate, then call resume_pipeline.")
        # Staging survives loop restarts; only bring it back if its process is gone.
        if self.cfg.staging_configured and self.staging.state().get("current_build_id"):
            threading.Thread(target=self.staging.ensure_running, daemon=True, name="staging").start()
        try:
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as e:  # keep the loop alive
                    log(f"tick error: {e!r}")
                if once and not self._busy():
                    break
                time.sleep(0.5 if once else 3)
        finally:
            if getattr(self, "desk", None):
                self.desk.stop.set()
            for aid, a in self.store.assignments().items():     # whoever holds work stops: no orphans
                self.events.publish(RELEASED, {"assignment": aid, "ticket": a["ticket"], "role": a["role"],
                                               "reason": "the task system shut down"}, subject=a["ticket"])
            # only drop the record if it is ours: a replacement may already have started
            self.store._mutate_runtime(lambda d: d.pop("scheduler", None) if (d.get("scheduler") or {}).get("pid") == os.getpid() else None)
            self.events.emit("loop_stopped")

    def _busy(self) -> bool:
        return bool(self.store.assignments()) or any(t.is_alive() for t in [*self.preparing.values(), self.gate_thread] if t)

    # -- tick ------------------------------------------------------------------

    def tick(self) -> None:
        self.project.reload_config()
        self.cfg = self.project.config
        self._reap()
        self._service_kills()
        self._follow()
        tickets = self.store.tickets_by_id()
        milestones = self.store.milestones_by_id()
        cap = self.store.capacity()
        eff = effective_priorities(tickets, self.cfg.aging_days_per_level)
        ordered = sorted(tickets.values(), key=lambda t: (eff.get(t.id, {}).get("effective", t.priority), t.created_at, t.id))
        paused = self.store.pickup_paused()
        out = self.store.assignments().values()
        busy = {r: sum(1 for a in out if a["role"] == r) for r in ("worker", "reviewer")}
        taken = {a["ticket"] for a in out}
        # Review before pick: finished work lands before new work starts.
        for t in ordered:
            if t.state == State.READY_FOR_REVIEW and busy["reviewer"] < cap["reviewers"] and t.id not in taken:
                if self._assign(t, "reviewer"):
                    busy["reviewer"] += 1
        for t in ordered:
            if paused or busy["worker"] >= cap["workers"]:
                break
            if t.id in taken:
                continue
            if t.state == State.REJECTED or (t.state == State.OPEN and is_ready(t, tickets, milestones)):
                if self._assign(t, "worker"):
                    busy["worker"] += 1
        if self.cfg.work == "code" and self.gate_thread is None and not self.store.pipeline().get("paused"):
            approved = [t for t in ordered if t.state == State.APPROVED]
            if approved:
                approved.sort(key=lambda t: t.state_since)
                self.gate_thread = threading.Thread(target=self._gate, args=([approved[0].id],), daemon=True)
                self.gate_thread.start()
        for t in ordered:
            if self.cfg.work == "code" and t.state == State.CANCELLED and self.store.worktree(t.id):
                self._drop_worktree(t)
        self.staging.ensure_running()
        try:
            self.prober.tick(config_probes(self.project))
        except Exception as e:
            log(f"probes: {e!r}")

    def _service_kills(self) -> None:
        """The operator stops whoever works a ticket: its assignment is released (the machine
        stops), and when its report comes back the ticket is reset without counting a crash."""
        for tid in self.store.take_kill_requests():
            for aid, a in self.store.assignments().items():
                if a["ticket"] == tid:
                    self.killed.add(tid)
                    log(f"{tid}: {a['role']} stopped by the operator")
                    self.events.publish(RELEASED, {"assignment": aid, "ticket": tid, "role": a["role"], "reason": "the operator stopped it"},
                                        subject=tid)
                    self.events.emit("agent_killed", ticket=tid, session=f"{a['role']}-{tid}")

    def _reap(self) -> None:
        for k in [k for k, th in self.preparing.items() if not th.is_alive()]:
            self.preparing.pop(k)
        if self.gate_thread and not self.gate_thread.is_alive():
            self.gate_thread = None

    # -- worker ----------------------------------------------------------------

    def _worktree(self, t: Ticket) -> tuple[Path, str]:
        branch = t.branch or f"orchestra/{t.id}-{t.slug}"
        path = self.project.worktrees_dir / t.id
        if not path.exists() or self.git.worktree_branch(path) != branch:
            self.git.worktree_remove(path)
            self.git.worktree_add(branch, path, self.cfg.base_branch)
        self.store.set_worktree(t.id, str(path), branch)
        return path, branch

    def _workspace(self, t: Ticket) -> Path:
        """A task's scratch folder (work = "tasks"): the worker's working directory, kept for the
        reviewer. Not a git checkout; nothing in it is merged anywhere."""
        path = self.project.workspaces_dir / t.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- assignment (tasking/assignments.py) -----------------------------------

    def _assign(self, t: Ticket, role: str) -> bool:
        """Offer a ticket to a role: get its place ready (a folder, or a branch in code mode),
        move it to IN_PROGRESS or IN_REVIEW, record the assignment with its lease, and say so on
        the bus. Whatever serves the role takes it; nobody owns the ticket until one accepts."""
        tasks = self.cfg.work != "code"
        try:
            if role == "worker":
                path, branch = (self._workspace(t), None) if tasks else self._worktree(t)
                t = self.store.transition(t.id, t.version, State.IN_PROGRESS, worker_turns=t.worker_turns + 1,
                                          **({} if tasks else {"branch": branch}))
            else:
                if tasks:
                    path, branch = self.project.workspaces_dir / t.id, None
                else:
                    wt = self.store.worktree(t.id)
                    path, branch = (Path(wt["path"]), wt.get("branch")) if wt else (None, None)
                t = self.store.transition(t.id, t.version, State.IN_REVIEW)
                if path is None or not path.is_dir():
                    self._reject(t.id, "orchestra", "the ticket's " + ("folder" if tasks else "worktree") + " is missing at review time; "
                                 "please do the work again", reviewer_text="")
                    return False
        except (Conflict, StoreError, GitError) as e:
            log(f"{t.id}: cannot assign a {role}: {e}")
            return False
        aid = new_id(t.id, role)
        offer = {"assignment": aid, "role": role, "ticket": t.id, "kind": "work" if role == "worker" else "review",
                 "workspace": str(path), "branch": branch, "lease_s": LEASE_S, "brief": brief(t)}
        if role == "worker" and not tasks and self.cfg.unit:
            offer["unit_command"] = self.cfg.unit[0].command
        record = {"id": aid, "ticket": t.id, "role": role, "workspace": str(path), "branch": branch,
                  "assigned_at": time.time(), "lease_until": time.time() + LEASE_S, "instance": None}
        if role == "reviewer" and not tasks:
            # verifying is the task system's: the unit tests run on the branch before it is offered,
            # so the lease starts when the offer goes out, not while the tests run
            self.store.put_assignment({**record, "lease_until": time.time() + 86400})
            th = threading.Thread(target=self._offer_review, args=(aid, t, path, offer), daemon=True, name=f"{aid}:prepare")
            self.preparing[aid] = th
            th.start()
            return True
        if role == "reviewer":
            offer["review"] = {"summary": self._worker_summary(t.id)}
        self.store.put_assignment(record)
        self.events.publish(ASSIGNED, offer, subject=t.id)
        log(f"{t.id}: assigned to a {role} ({aid})")
        return True

    def _worker_summary(self, tid: str) -> str:
        return next((n.get("text", "") for n in reversed(self.store.notes(tid)) if n.get("author") == "worker"), "")

    def _offer_review(self, aid: str, t: Ticket, path: Path, offer: dict) -> None:
        """A code review's offer: the worker's summary, the commit to review, and the unit tests'
        output on it, then the assignment goes out and its lease starts."""
        test_output = None
        try:
            if self.cfg.unit:
                _ok, res = run_specs(self.cfg.unit, path, self.project.logs_dir / "review" / t.id, stage="unit")
                test_output = "\n".join(f"$ {r['command']}\n{r['tail']}\n[{r['status']}]" for r in res)
            commit = t.reviewed_commit or self.git.rev_parse("HEAD", cwd=path)
        except (GitError, OSError) as e:
            log(f"{t.id}: cannot prepare the review: {e}")
            commit = t.reviewed_commit
        if aid not in self.store.assignments():                   # released meanwhile (the operator, a shutdown)
            return
        offer["review"] = {"summary": self._worker_summary(t.id), "commit": commit, "test_output": test_output}
        self.store.update_assignment(aid, lease_until=time.time() + LEASE_S)
        self.events.publish(ASSIGNED, offer, subject=t.id)
        log(f"{t.id}: assigned to a reviewer ({aid})")

    def _follow(self) -> None:
        """What the assignees said since the last tick, applied: an acceptance names the owner, a
        heartbeat extends the lease, a report is applied to the ticket. Then any lease that ran out
        is released and its ticket goes back, as a crash."""
        out = self.store.assignments()
        for ev in self.follow.new():
            aid = ev.get("assignment")
            if ev.get("type") not in (ACCEPTED, WORKING, REPORTED) or aid not in out:
                continue
            a = out[aid]
            if ev["type"] == ACCEPTED and a.get("instance") and a["instance"] != ev.get("instance"):
                # two machines answered: the first keeps it, the other is told to stop
                self.events.publish(RELEASED, {"assignment": aid, "ticket": a["ticket"], "role": a["role"], "instance": ev.get("instance"),
                                               "reason": f"already taken by {a['instance']}"}, subject=a["ticket"])
                continue
            if ev["type"] == ACCEPTED:
                a = self.store.update_assignment(aid, instance=ev.get("instance"), session=ev.get("session"),
                                                 lease_until=time.time() + LEASE_S) or a
                t = self.store.get_ticket(a["ticket"])
                if t.state in ACTIVE:                                     # the owner, and the session it works in, if it has one
                    self.store.update_ticket(t.id, None, owner=ev.get("instance"),
                                             **({f"{a['role']}_session": ev["session"]} if ev.get("session") else {}))
            elif ev["type"] == WORKING and ev.get("source") in (a.get("instance"), None):
                self.store.update_assignment(aid, lease_until=time.time() + LEASE_S)
            elif ev["type"] == WORKING:
                continue
            elif a.get("instance") and ev.get("source") not in (a["instance"],):
                continue                                                  # a report from a machine that does not hold it
            else:
                self.store.drop_assignment(aid)
                out.pop(aid, None)
                self._apply(a, ev.get("report"), ev.get("text") or "", bool(ev.get("failed")), ev.get("commit"))
        now_t = time.time()
        for aid, a in self.store.assignments().items():
            if a["lease_until"] < now_t:
                self.store.drop_assignment(aid)
                self.events.publish(RELEASED, {"assignment": aid, "ticket": a["ticket"], "role": a["role"],
                                               "reason": f"no word from {a.get('instance') or 'any ' + a['role']} for {int(LEASE_S)} s"},
                                    subject=a["ticket"])
                log(f"{a['ticket']}: {a['role']} assignment {aid} released: the lease ran out")
                self._apply(a, None, "", True, None)

    def _apply(self, a: dict, report: dict | None, text: str, failed: bool, commit: str | None) -> None:
        path = Path(a["workspace"]) if a.get("workspace") else None
        if a["role"] == "worker":
            self._apply_worker_report(a["ticket"], report, text, failed, path, a.get("branch"))
        else:
            self._apply_review(a["ticket"], report, text, failed, commit)

    def _apply_worker_report(self, tid: str, report: dict | None, text: str, failed: bool,
                             path: Path, branch: str) -> None:
        t = self.store.get_ticket(tid)
        if t.state != State.IN_PROGRESS:
            return
        if tid in self.killed:
            self.killed.discard(tid)
            self.store.add_note(tid, Note(author="orchestra", kind="note", text="worker stopped by the operator; ticket reset to OPEN"))
            self.store.transition(tid, t.version, State.OPEN, feedback="Your previous turn was stopped by the operator. Check `git status` and `git log`, then continue from where you left off and report.")
            log(f"{tid}: reset to OPEN after operator stop")
            return
        if report is None and self.stop.is_set():
            # The orchestrator is shutting down (restart or redeploy): that is not the
            # worker's fault, so reset without counting a crash.
            self.store.add_note(tid, Note(author="orchestra", kind="note", text="orchestrator shut down during this turn; ticket reset to OPEN"))
            self.store.transition(tid, t.version, State.OPEN, feedback="Your previous turn was interrupted by an orchestrator restart. Check `git status` and `git log`, continue from where you left off, then report.")
            return
        if report is None:
            # Count only the trailing run of crash notes, so an unblock or any other note
            # resets the budget instead of a ticket carrying old crashes forever.
            crashes = 1 if failed else 0
            for n in reversed(self.store.notes(tid)):
                if n.get("kind") != "crash":
                    break
                crashes += 1
            if failed and crashes <= 2:
                # The worker process died or timed out (often an orchestrator restart). That is
                # not a decision for a human: keep the session and branch, retry from OPEN.
                self.store.add_note(tid, Note(author="orchestra", kind="crash",
                                              text=f"worker process ended without reporting (attempt {crashes}); retrying from its session"))
                self.store.transition(tid, t.version, State.OPEN, feedback="Your previous turn ended unexpectedly. Check `git status` and `git log`, continue from where you left off, then report.")
                log(f"{tid}: worker died; back to OPEN for retry ({crashes})")
                return
            reason = ("the worker process died or timed out repeatedly" if failed else "worker stopped without calling report_ticket")
            reason += f". Last message: {text[-600:]}" if text else "."
            self.store.add_note(tid, Note(author="orchestra", kind="blocker", text=reason))
            self.store.transition(tid, t.version, State.BLOCKED, blocked_reason=reason)
            self.events.attention("coordinator", f"{tid} BLOCKED: {reason[:200]}", ticket=tid)
            log(f"{tid}: BLOCKED ({reason[:80]})")
            return
        self.store.add_note(tid, Note(author="worker", text=report.get("summary", "")))
        if report["state"] == "BLOCKED":
            self.store.transition(tid, t.version, State.BLOCKED, blocked_reason=report.get("blocked_reason"))
            self.events.attention("coordinator", f"{tid} BLOCKED by worker: {report.get('blocked_reason', '')[:200]}", ticket=tid)
            log(f"{tid}: BLOCKED by worker")
            return
        # READY_FOR_REVIEW: make sure there is something to review.
        if branch is None:                                   # a task: its result is the report and RESULT.md
            self.store.transition(tid, t.version, State.READY_FOR_REVIEW, feedback=None)
            log(f"{tid}: READY_FOR_REVIEW")
            return
        if self.git.run("status", "--porcelain", cwd=path, check=False).stdout.strip():
            self.git.run("add", "-A", cwd=path, check=False)
            self.git.run("commit", "-q", "-m", f"{tid}: commit remaining worker changes", cwd=path, check=False)
        head = self.git.rev_parse("HEAD", cwd=path)
        if head == self.git.rev_parse(self.cfg.base_branch) or self.git.is_ancestor(head, self.cfg.base_branch):
            reason = "worker reported READY_FOR_REVIEW but the branch has no commits beyond the base branch"
            self.store.add_note(tid, Note(author="orchestra", kind="blocker", text=reason))
            self.store.transition(tid, t.version, State.BLOCKED, blocked_reason=reason)
            self.events.attention("coordinator", f"{tid} BLOCKED: {reason}", ticket=tid)
            return
        self.store.transition(tid, t.version, State.READY_FOR_REVIEW, reviewed_commit=head, feedback=None)
        log(f"{tid}: READY_FOR_REVIEW at {head[:10]}")

    # -- reviewer --------------------------------------------------------------

    def _apply_review(self, tid: str, report: dict | None, text: str, failed: bool, commit: str | None) -> None:
        """A reviewer's report, applied: its verdict, or (none came) the review is redone, up to
        three times in a row before the ticket goes back to its worker."""
        t = self.store.get_ticket(tid)
        if t.state != State.IN_REVIEW:
            return
        if tid in self.killed:
            self.killed.discard(tid)
            self.store.add_note(tid, Note(author="orchestra", kind="note", text="reviewer stopped by the operator; review will be redone"))
            self.store.transition(tid, t.version, State.READY_FOR_REVIEW)
            return
        if report is None:
            # A reviewer that produced no verdict is not a rejection: the worker has nothing to
            # fix. The review is redone, up to three times in a row, before the ticket is sent
            # back; and that send-back is the scheduler's, never counted as a reviewer's verdict.
            tries = 1
            for n in reversed(self.store.notes(tid)):
                if n.get("kind") != "crash" or not n.get("text", "").startswith("reviewer"):
                    break
                tries += 1
            if tries <= 3:
                self.store.add_note(tid, Note(author="orchestra", kind="crash",
                                              text=f"reviewer produced no verdict{' (timed out)' if failed else ''} (attempt {tries}); review will be redone"))
                self.store.transition(tid, t.version, State.READY_FOR_REVIEW)
                log(f"{tid}: reviewer produced no verdict; review will be redone ({tries})")
                return
            self._reject(tid, "orchestra", "three reviewers in a row produced no verdict" + (" (timed out)" if failed else "") +
                         ". Make the work small and focused so a review can finish, and report again.", text)
            return
        self._verdict(t, report, text, commit)

    def _verdict(self, t: Ticket, report: dict, text: str, commit: str | None) -> None:
        self.store.add_note(t.id, Note(author="reviewer", kind="review",
                                       text=f"{report['verdict']}: {report.get('summary', '')}\n{report.get('feedback') or ''}"))
        if report["verdict"] == "APPROVED":
            t = self.store.transition(t.id, t.version, State.APPROVED, reviewed_commit=commit, feedback=None, rejected_by=None)
            log(f"{t.id}: APPROVED")
            if self.cfg.work != "code":                       # nothing to merge: approved is done
                self.store.transition(t.id, t.version, State.DONE)
                log(f"{t.id}: DONE")
                self.events.attention("coordinator", f"{t.id} is DONE", ticket=t.id)
        else:
            self._reject(t.id, "reviewer", report.get("feedback") or report.get("summary", ""), text)

    def _reject(self, tid: str, by: str, feedback: str, reviewer_text: str) -> None:
        t = self.store.get_ticket(tid)
        rounds = t.review_rounds + 1
        self.store.transition(tid, t.version, State.REJECTED, rejected_by="reviewer" if by != "merge_gate" else by,
                              feedback=feedback, review_rounds=rounds,
                              reviewer_rejections=t.reviewer_rejections + (1 if by == "reviewer" else 0))
        log(f"{tid}: REJECTED (round {rounds})")
        # Escalate when the threshold is crossed, and again at every further multiple of it,
        # not on every rejection: the Coordinator should decide once, not be told 38 times.
        th = self.cfg.review_rounds_before_escalation
        if th and rounds % th == 0:
            self.events.attention("coordinator", f"{tid} has been rejected {rounds} times; decide whether to narrow, split, or cancel it", ticket=tid)

    # -- gate ------------------------------------------------------------------

    def _gate(self, ticket_ids: list[str]) -> None:
        try:
            for tid in ticket_ids:
                t = self.store.get_ticket(tid)
                self.store.transition(tid, t.version, State.MERGING)
            log(f"gate: {', '.join(ticket_ids)}")
            run = run_gate(self.project, self.store, self.events, ticket_ids)
            log(f"gate {run['id']}: {run['status']} {run.get('summary', '')}")
            if run["status"] == "landed":
                self.events.attention("coordinator", f"landed {', '.join(run['merged'])} on {self.cfg.base_branch}")
        except (Conflict, StoreError) as e:
            log(f"gate: {e}")

    def _drop_worktree(self, t: Ticket) -> None:
        wt = self.store.worktree(t.id)
        if wt:
            self.git.worktree_remove(Path(wt["path"]))
            self.store.remove_worktree(t.id)
