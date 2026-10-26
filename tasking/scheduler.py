"""The task system's scheduler: it offers ready tickets to the roles that work them (a worker, a
reviewer) over Orchestra's bus, follows what the machines that took them say, lands approved
code through the merge gate, and runs the probes in ``orchestra.toml``. It starts no machine:
whatever fills a role answers (tasking/assignments.py). What a report makes of a ticket is
tasking/reports.py. SPEC.md section 4."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable
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
from .events import COORDINATOR, SCHEDULER, SUPERVISOR, Events
from .gate import run_gate
from .gate.runner import run_specs
from .git import Git, GitError
from .model import ACTIVE, State, Ticket, TransitionError, effective_priorities, is_ready, now
from .probes import Prober, config_probes
from .reports import Reports
from .requests import QUERY, REQUESTS, Desk
from .staging import Staging
from .store import Conflict, LocalStore, StoreError

LOG: Path | None = None               # <state>/logs/scheduler.log once a scheduler starts
LOG_MAX = 5_000_000                   # bytes; the file is rotated to .1 past this
OFFERED_FROM = {"worker": {State.OPEN, State.REJECTED}, "reviewer": {State.READY_FOR_REVIEW}}   # the states an offer is made from


def log(msg: str) -> None:
    line = f"[tasking {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if LOG is not None:                 # the scheduler's log, announced to Orchestra's Machines page
        try:
            if LOG.exists() and LOG.stat().st_size > LOG_MAX:
                LOG.replace(LOG.with_suffix(".log.1"))
            with LOG.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


class Scheduler:
    def __init__(self, project: Project, store: LocalStore, events: Events):
        global LOG
        LOG = project.logs_dir / "scheduler.log"
        LOG.parent.mkdir(parents=True, exist_ok=True)
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
        self.reports = Reports(project, store, events, self.git, log)
        self.follow = Follower(events)               # what the assignees say back (tasking/assignments.py)

    def recover(self) -> None:
        """What the previous scheduler left: whoever still holds work is told to stop and every
        active ticket goes back to waiting (no zombies); the gate's leftovers are removed."""
        self._release_all("the task system restarted")
        for run in self.store.gate_runs():
            if run.get("status") == "running":
                run["status"] = "aborted"
                run["summary"] = "the task system restarted during the run"
                run["finished_at"] = run.get("finished_at") or now()
                self.store.save_gate_run(run)
        for p in self.project.gate_dir.glob("*"):
            self.git.worktree_remove(p)
        self.git.run("worktree", "prune", check=False)
        for b in self.git.run("branch", "--list", "orchestra/gate-*", check=False).stdout.split():
            self.git.branch_delete(b.strip("* "))

    def _release_all(self, reason: str) -> None:
        """Every assignment released and settled, then every ticket still active sent back: the
        store never claims work is held when nobody holds it. An offer nobody accepted is only
        dropped: it changed nothing."""
        for a in self.store.assignments().values():
            self._release(a, reason)
            if a.get("instance") is not None:
                self._settle(a, lambda a=a: self.reports.back_to_waiting(a["ticket"], f"{reason} during the {a['role']}'s turn",
                                                                       f"{reason} during the previous turn; the workspace is as it was left"))
        for t in self.store.list_tickets():
            if t.state in ACTIVE:
                self.reports.back_to_waiting(t.id, reason)

    def _release(self, a: dict, reason: str, instance: str | None = None) -> None:
        """``task.released`` for an assignment (to one instance when the release is addressed to
        it); an offer nobody accepted is dropped here, since nothing is counted for it."""
        data = {"assignment": a["id"], "ticket": a["ticket"], "role": a["role"], "reason": reason}
        if instance is not None:
            data["instance"] = instance
        self.events.publish(RELEASED, data, subject=a["ticket"])
        if a.get("instance") is None and instance is None:
            self.store.drop_assignment(a["id"])

    def run(self, once: bool = False) -> None:
        def _stop(_sig, _frame):
            self.stop.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _stop)
            except ValueError:  # not the main thread (tests)
                pass
        self.recover()
        self.desk = Desk(self.project, self.store, self.events, log)   # planners' requests (tasking/requests.py)
        self.desk.start()
        # the record says the scheduler is up: by then the desk listens, so a request sent now is heard
        self.store.set_scheduler(os.getpid(), time.time())
        self.events.emit("scheduler.started")
        self.events.publish("orchestra.machine.started", {"package": "tasks", "mode": "continuous", "kind": "program",
                                                          "listens_for": sorted({*REQUESTS.values(), QUERY, ACCEPTED, WORKING, REPORTED}),
                                                          "log": str(LOG),
                                                          "does": "offers ready tickets to workers and reviewers and applies their reports"})
        log(f"project {self.project.key} on {self.project.repo_root}; workers={self.cfg.workers} reviewers={self.cfg.reviewers}")
        paused = self.store.pipeline()
        if paused.get("paused"):
            self.events.attention(SUPERVISOR, f"the pipeline is paused after a restart: {paused.get('reason')}; "
                                  "it resumes on pipeline.resume_requested")
        # staging survives scheduler restarts; only bring it back if its process is gone
        if self.cfg.staging_configured and self.staging.state().get("current_build_id"):
            threading.Thread(target=self.staging.ensure_running, daemon=True, name="staging").start()
        try:
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001 - one bad tick never stops the scheduler
                    log(f"tick error: {e!r}")
                if once and not self._busy():
                    break
                time.sleep(0.5 if once else 3)
        finally:
            self.desk.stop.set()
            self._finish_threads()
            self._release_all("the task system shut down")
            self.store.clear_scheduler(os.getpid())
            self.events.emit("scheduler.stopped")

    def _busy(self) -> bool:
        return bool(self.store.assignments()) or any(t.is_alive() for t in [*self.preparing.values(), self.gate_thread] if t)

    def _finish_threads(self) -> None:
        """A gate run lands or fails before the shutdown releases anything, so a ticket the gate
        is landing is never sent back under it; the reviews being prepared finish their tests."""
        if self.gate_thread and self.gate_thread.is_alive():
            log("waiting for the gate run to finish")
            self.gate_thread.join(timeout=self.cfg.gate_timeout + self.cfg.build_timeout + self.cfg.acceptance_ready_timeout)
        for th in self.preparing.values():
            th.join(timeout=sum(s.timeout for s in self.cfg.unit))

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
        # review before pick: finished work lands before new work starts
        for t in ordered:
            if t.state == State.READY_FOR_REVIEW and busy["reviewer"] < cap["reviewers"] and t.id not in taken \
                    and self._assign(t, "reviewer"):
                busy["reviewer"] += 1
        for t in ordered:
            if paused or busy["worker"] >= cap["workers"]:
                break
            if t.id in taken:
                continue
            if (t.state == State.REJECTED or (t.state == State.OPEN and is_ready(t, tickets, milestones))) \
                    and self._assign(t, "worker"):
                busy["worker"] += 1
        if self.cfg.work == "code" and self.gate_thread is None and not self.store.pipeline().get("paused"):
            approved = [t for t in ordered if t.state == State.APPROVED]
            if approved:
                approved.sort(key=lambda t: t.state_since)
                self.gate_thread = threading.Thread(target=self._gate, args=(approved[0].id,), daemon=True)
                self.gate_thread.start()
        for t in ordered:
            if self.cfg.work == "code" and t.state == State.CANCELLED and self.store.worktree(t.id):
                self._drop_worktree(t)
        self.staging.ensure_running()
        try:
            self.prober.tick(config_probes(self.project))
        except Exception as e:  # noqa: BLE001 - a broken probe never stops the scheduler
            log(f"probes: {e!r}")

    def _service_kills(self) -> None:
        """The operator stops whoever works a ticket: its assignment is released (the machine
        stops) and the ticket goes back to waiting at once, without counting against anyone. An
        offer nobody accepted yet is only withdrawn."""
        for tid in self.store.take_kill_requests():
            for a in self.store.assignments().values():
                if a["ticket"] == tid:
                    log(f"{tid}: {a['role']} stopped by the operator")
                    self._release(a, "the operator stopped it")
                    if a.get("instance") is not None:
                        self._settle(a, lambda a=a: self.reports.stopped(a))

    def _reap(self) -> None:
        for k in [k for k, th in self.preparing.items() if not th.is_alive()]:
            self.preparing.pop(k)
        if self.gate_thread and not self.gate_thread.is_alive():
            self.gate_thread = None

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

    def _assign(self, t: Ticket, role: str) -> bool:
        """Offer a ticket to a role: get its place ready (a folder, or a branch in code mode),
        record the assignment with its lease, and say so on the bus. Whatever serves the role
        takes it; the ticket moves (IN_PROGRESS, IN_REVIEW) and gets its owner when one accepts
        (``_follow``), so an offer nobody takes changes nothing."""
        tasks = self.cfg.work != "code"
        try:
            if role == "worker":
                path, branch = (self._workspace(t), None) if tasks else self._worktree(t)
            else:
                if tasks:
                    path, branch = self.project.workspaces_dir / t.id, None
                else:
                    wt = self.store.worktree(t.id)
                    path, branch = (Path(wt["path"]), wt.get("branch")) if wt else (None, None)
                if path is None or not path.is_dir():
                    self.reports.reject(t.id, SCHEDULER, "the ticket's " + ("folder" if tasks else "worktree")
                                        + " is missing at review time; the work has to be done again", counted=False)
                    return False
        except (Conflict, StoreError, GitError) as e:
            log(f"{t.id}: cannot assign a {role}: {e}")
            return False
        aid = new_id(t.id, role)
        turns = {"worker_turns": t.worker_turns + 1, "branch": branch} if role == "worker" else {}   # as the ticket will read once accepted
        offer = {"assignment": aid, "role": role, "ticket": t.id, "kind": "work" if role == "worker" else "review",
                 "workspace": str(path), "branch": branch, "lease_s": LEASE_S, "brief": {**brief(t), **turns}}
        if role == "worker" and not tasks and self.cfg.unit:
            offer["unit_command"] = self.cfg.unit[0].command
        record = {"id": aid, "ticket": t.id, "role": role, "workspace": str(path), "branch": branch,
                  "assigned_at": time.time(), "lease_until": time.time() + LEASE_S, "instance": None, "accepted_at": None}
        if role == "reviewer" and not tasks:
            # verifying is the task system's: the unit tests run on the branch before the review is
            # offered, so the lease starts when the offer goes out; until then the record is held
            # for as long as the tests may take
            preparing = sum(s.timeout for s in self.cfg.unit) + LEASE_S
            self.store.put_assignment({**record, "lease_until": time.time() + preparing})
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
        output on it, then the assignment goes out and its lease starts. A review that cannot be
        prepared is not offered: the record is dropped and the ticket waits for a reviewer again."""
        try:
            test_output = None
            if self.cfg.unit:
                _ok, res = run_specs(self.cfg.unit, path, self.project.logs_dir / "review" / t.id, stage="unit")
                test_output = "\n".join(f"$ {r['command']}\n{r['tail']}\n[{r['status']}]" for r in res)
            commit = t.reviewed_commit or self.git.rev_parse("HEAD", cwd=path)
        except Exception as e:  # noqa: BLE001 - whatever failed, the ticket must not stay held
            log(f"{t.id}: cannot prepare the review: {e!r}")
            self.store.drop_assignment(aid)
            return
        if aid not in self.store.assignments():                   # released meanwhile (the operator, a shutdown)
            return
        offer["review"] = {"summary": self._worker_summary(t.id), "commit": commit, "test_output": test_output}
        self.store.update_assignment(aid, lease_until=time.time() + LEASE_S)
        self.events.publish(ASSIGNED, offer, subject=t.id)
        log(f"{t.id}: assigned to a reviewer ({aid})")

    def _follow(self) -> None:
        """What the assignees said since the last tick, applied: the first acceptance names the
        holder and moves the ticket, a heartbeat extends the lease, a report is applied to the
        ticket; anything from a machine that does not hold the assignment is ignored. Then every
        lease that ran out is released: a holder that went quiet counts as a crash; an offer
        nobody took is dropped and offered again."""
        out = self.store.assignments()
        for ev in self.follow.new():
            aid = ev.get("assignment")
            if ev.get("type") not in (ACCEPTED, WORKING, REPORTED) or aid not in out:
                continue
            a = out[aid]
            if ev["type"] == ACCEPTED:
                if a.get("instance") is None:
                    if self._accept(a, ev):
                        out[aid] = self.store.assignments().get(aid, a)
                    else:
                        out.pop(aid, None)
                elif a["instance"] != ev["source"]:
                    # two machines answered: the first keeps it, the other is told to stop
                    self._release(a, f"already taken by {a['instance']}", instance=ev["source"])
                continue
            if ev.get("source") != a.get("instance"):
                continue
            if ev["type"] == WORKING:
                self.store.update_assignment(aid, lease_until=time.time() + LEASE_S)
            else:
                out.pop(aid)
                self._settle(a, lambda a=a, ev=ev: self.reports.apply(a, ev.get("report"), ev.get("text") or "", bool(ev.get("failed")), ev.get("commit")))
        now_t = time.time()
        for aid, a in self.store.assignments().items():
            if a.get("instance") is None:
                state = self.store.get_ticket(a["ticket"]).state
                if a["lease_until"] < now_t:
                    reason = f"nobody filled the role {a['role']} within {int(LEASE_S)} s"
                    self._release(a, reason)
                    log(f"{a['ticket']}: assignment {aid} released: {reason}")
                elif state not in OFFERED_FROM[a["role"]]:                                # cancelled meanwhile
                    self._release(a, f"the ticket is {state}")
            elif a["lease_until"] < now_t:
                self._release(a, f"no word from {a['instance']} for {int(LEASE_S)} s")
                log(f"{a['ticket']}: {a['role']} assignment {aid} released: the lease ran out")
                self._settle(a, lambda a=a: self.reports.apply(a, None, "", True, None))

    def _accept(self, a: dict, ev: dict) -> bool:
        """The first acceptance of an offer: the holder is the event's source (an ``instance``
        that says otherwise is refused and the offer stays open), and the ticket moves to the
        role's active state with that owner. A ticket that can no longer move (cancelled
        meanwhile) is released to the instance and the offer dropped. True while the offer stands."""
        holder, claimed = ev["source"], ev.get("instance")
        if claimed is not None and claimed != holder:
            self._release(a, f"instance must be the event's source ({holder})", instance=claimed)
            return True
        t = self.store.get_ticket(a["ticket"])
        fields = ({"worker_turns": t.worker_turns + 1, **({"branch": a["branch"]} if a.get("branch") else {})}
                  if a["role"] == "worker" else {})
        try:
            self.store.transition(t.id, t.version, State.IN_PROGRESS if a["role"] == "worker" else State.IN_REVIEW, owner=holder, **fields)
        except (Conflict, StoreError, TransitionError) as e:
            self._release(a, f"the ticket cannot be {'worked' if a['role'] == 'worker' else 'reviewed'} now: {e}", instance=holder)
            self.store.drop_assignment(a["id"])
            log(f"{t.id}: {holder} accepted {a['id']} but {e}; released")
            return False
        self.store.update_assignment(a["id"], instance=holder, accepted_at=time.time(), lease_until=time.time() + LEASE_S)
        log(f"{t.id}: held by {holder}")
        return True

    def _settle(self, a: dict, outcome: Callable[[], None]) -> None:
        """An assignment's outcome applied, then the assignment dropped, whatever happened: an
        outcome the task system cannot apply still frees the ticket (back to waiting, with a note)
        instead of leaving it held by nobody."""
        try:
            outcome()
        except Exception as e:  # noqa: BLE001 - the ticket must not stay active without a holder
            log(f"{a['ticket']}: cannot apply the {a['role']}'s outcome: {e!r}")
            self.reports.back_to_waiting(a["ticket"], f"the task system could not apply the {a['role']}'s report ({e})")
        finally:
            self.store.drop_assignment(a["id"])

    def _gate(self, ticket_id: str) -> None:
        try:
            t = self.store.get_ticket(ticket_id)
            self.store.transition(ticket_id, t.version, State.MERGING)
            log(f"gate: {ticket_id}")
            run = run_gate(self.project, self.store, self.events, ticket_id)
            log(f"gate {run['id']}: {run['status']} {run.get('summary', '')}")
            if run["status"] == "landed":
                self.events.attention(COORDINATOR, f"landed {ticket_id} on {self.cfg.base_branch}", ticket=ticket_id)
        except (Conflict, StoreError, TransitionError) as e:
            log(f"gate: {e}")

    def _drop_worktree(self, t: Ticket) -> None:
        wt = self.store.worktree(t.id)
        if wt:
            self.git.worktree_remove(Path(wt["path"]))
            self.store.remove_worktree(t.id)
