"""The task system's scheduler: the record, not the hub (the owner's decision D17), and no
machine. One daemon stands in for each machine's own scheduler: the worker's queue (offering
waiting tickets, taking back what a silent worker holds), the reviewer's, the merge gate
(``merge_gate``, landing approved code), the answers to a planner's requests (tasking/requests.py)
the probes in ``orchestra.toml``, and the planner's recurring tickets (``planner/recurring``,
tasking/recurring.py). The queue is the coordinator's dispatch (choosing what is
offered next: readiness, priority, capacity), so its offers go out as ``coordinator/dispatch``. So it never announces itself and never publishes as itself:
what it publishes goes out as the machine whose responsibility it implements at that moment
(tasking/events.py; SPEC.md section 3, "Who publishes what"). It follows what the machines say to
each other as the work passes between them (a review requested, changes requested, an approval)
and records it, publishing nothing for it. It starts no machine and relays nothing: whatever fills
a role answers (tasking/assignments.py). What an event makes of a ticket is tasking/reports.py.
SPEC.md sections 3 and 4."""

from __future__ import annotations

import contextlib
import os
import signal
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from orchestra.events import machine_and_instance
from orchestra.runtime import alive

from .assignments import (
    ACCEPTED,
    APPROVED,
    ASSIGNED,
    CHANGES_REQUESTED,
    LEASE_S,
    RELEASED,
    REPORTED,
    REVIEW_REQUESTED,
    WORKING,
    brief,
    new_id,
)
from .config import Project
from .events import DISPATCH, GATE, SCHEDULER, SUPERVISOR, Events
from .external import External
from .gate import run_gate
from .git import Git, GitError
from .model import ACTIVE, Note, State, Ticket, TransitionError, effective_priorities, is_ready, now
from .pool import Pool
from .probes import Prober, config_probes
from .recurring import Recurring
from .reports import Reports
from .requests import Desk
from .setup import Setup, SetupError
from .stacks import CONFLICT as STACK_CONFLICT
from .stacks import Stacks
from .staging import Staging
from .store import Conflict, LocalStore, StoreError

LOG: Path | None = None               # <state>/logs/scheduler.log once a scheduler starts
LOG_MAX = 5_000_000                   # bytes; the file is rotated to .1 past this
OFFERED_FROM = {"worker": {State.OPEN, State.REJECTED}, "reviewer": {State.READY_FOR_REVIEW}}   # the states an offer is made from
AGAIN_FIRST_S = 60.0                  # an offer nobody took within its lease goes out again after this pause,
AGAIN_MAX_S = 900.0                   # doubled with each offer nobody took, up to this
MACHINE_STARTED = "orchestra.machine.started"
# what makes dispatch offer work: a new ticket, work the gate sent back, work taken back from a role
DISPATCH_LISTENS = ["gate.conflict", "gate.rejected", RELEASED, "ticket.created"]
PROBES = "probes"                     # the source the [[probes]] are published as: the condition checks, no other machine's
ENDS = (REVIEW_REQUESTED, CHANGES_REQUESTED, APPROVED)    # they end the turn ``ends`` names
FOLLOWED = (ACCEPTED, WORKING, REPORTED, *ENDS)
SETTLED_KEPT = 1000                   # the turns lately settled, so a late claim of one is not answered (it lost)


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
        # the gate is a machine of its own (merge_gate): its events, and the record's changes it makes, are its
        self.gate_events = Events.for_project(project, source=GATE)
        self.gate_store = LocalStore(project, self.gate_events)
        self.settled: deque[str] = deque(maxlen=SETTLED_KEPT)
        self.stop = threading.Event()
        self.staging = Staging(project, self.gate_store, self.gate_events)     # deploys what the gate landed: the gate's
        self.prober = Prober(project, store, Events.for_project(project, source=PROBES))
        self.reports = Reports(project, store, events, self.git, log)
        self.recurring = Recurring(project, store, events, log)    # [[tasks.recurring]]: the planner's scheduled engagement
        # the pool is used only with [tasks] reuse_worktrees; a listed worktree it refuses, or a
        # worktree whose [tasks] worktree_setup fails, is the supervisor's to raise with the owner
        say = lambda reason: self.events.attention(SUPERVISOR, reason, src=DISPATCH)
        self.setup = Setup(project, log, attention=say)
        self.pool = Pool(project, store, self.git, log, attention=say, setup=self.setup)
        self.pool_waiting: set[tuple[str, str]] = set()     # (ticket, role or "gate") waiting for a free worktree
        self.offset = events.end()                   # read on from here: what the assignees say back (tasking/assignments.py)
        # external review: tickets follow their pull requests (tasking/external.py); it reads the bus on from its own record
        self.external = External(project, store, events, self.gate_store, self.gate_events, self.git,
                                 offer=self._offer_back, withdraw=self._withdraw, log=log)
        # with [gate] stacking: dependent tickets' branches stack on their dependency's, kept restacked (tasking/stacks.py)
        self.stacks = Stacks(project, store, self.git, self.gate_store, self.gate_events, log)
        self.external.base_of = self.stacks.base

    def recover(self) -> None:
        """What the previous scheduler left: whoever still holds work is told to stop and every
        active ticket goes back to waiting (no zombies); the gate's leftovers are removed."""
        self._release_all("the task system restarted")
        self.store.forget_untaken()                  # whoever serves the roles now gets fresh offers
        for run in self.store.gate_runs():
            if run.get("status") == "running":
                run["status"] = "aborted"
                run["summary"] = "the task system restarted during the run"
                run["finished_at"] = run.get("finished_at") or now()
                self.store.save_gate_run(run)
        self.store.release_gate_slots()
        if self.cfg.work == "code" and self.cfg.reuse_worktrees and self.cfg.worktree_paths:
            self.pool.survey()                       # the listed worktrees adopted, or refused and said so, at start
        for p in self.project.gate_dir.glob("*"):
            self.git.worktree_remove(p)
        self.git.run("worktree", "prune", check=False)
        for b in self.git.run("branch", "--list", "--format=%(refname:short)", "orchestra/gate-*", check=False).stdout.split():
            with contextlib.suppress(GitError):
                self.pool.release_branch(b)          # a reused worktree a gate run left on it
            self.git.branch_delete(b)

    def _release_all(self, reason: str) -> None:
        """Every assignment released and settled, then every ticket still active sent back: the
        store never claims work is held when nobody holds it. An offer nobody accepted is only
        dropped: it changed nothing."""
        for a in self.store.assignments().values():
            self._release(a, reason)
            if a.get("instance") is not None:
                self._settle(a, lambda a=a: self.reports.back_to_waiting(a["ticket"], f"{reason} during the {a['role']}'s turn",
                                                                       f"{reason} during the previous turn; {self.reports.workspace_kept()}"))
        for t in self.store.list_tickets():
            if t.state in ACTIVE:
                self.reports.back_to_waiting(t.id, reason)

    def _release(self, a: dict, reason: str, instance: str | None = None) -> None:
        """``task.released`` for an assignment (to one instance when the release is addressed to
        it). A turn an instance held, or a claim addressed back to its instance, is the role's
        (published as ``worker`` or ``reviewer``: that instance stops); an offer nobody claimed is
        withdrawn by dispatch, back into its queue (``coordinator/dispatch``), whoever made it, and
        dropped here, since nothing is counted for it."""
        data = {"assignment": a["id"], "ticket": a["ticket"], "role": a["role"], "reason": reason}
        if instance is not None:
            data["instance"] = instance
        unclaimed = a.get("instance") is None and instance is None
        self.events.publish(RELEASED, data, source=DISPATCH if unclaimed else a["role"], subject=a["ticket"])
        if unclaimed:
            self.store.drop_assignment(a["id"])

    def _other(self) -> int | None:
        """The pid of another live scheduler of this project, if one runs."""
        pid = (self.store.scheduler() or {}).get("pid")
        return pid if pid and pid != os.getpid() and alive(pid) else None

    def run(self, once: bool = False, wait: bool = False) -> None:
        """One scheduler per project: with another running, exit (or, ``wait``, as when
        ``orchestra run`` starts it, wait until that one stops and take over)."""
        def _stop(_sig, _frame):
            self.stop.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _stop)
            except ValueError:  # not the main thread (tests)
                pass
        if (other := self._other()) is not None:
            log(f"a scheduler (pid {other}) already runs {self.project.key}; " + ("waiting for it to stop" if wait else "not starting"))
            if not wait:
                raise SystemExit(1)
            while self._other() is not None:
                if self.stop.wait(2):
                    return
        self.recover()
        self.desk = Desk(self.project, self.store, self.events, log)   # planners' requests (tasking/requests.py)
        self.desk.start()
        # the record says the scheduler is up: by then the desk listens, so a request sent now is heard.
        self.store.set_scheduler(os.getpid(), time.time())
        self.announce()
        log(f"project {self.project.key} on {self.project.repo_root}; workers={self.cfg.workers} reviewers={self.cfg.reviewers}")
        paused = self.store.pipeline()
        if paused.get("paused"):
            self.gate_events.attention(SUPERVISOR, f"the pipeline is paused after a restart: {paused.get('reason')}; "
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
                time.sleep(0.5 if once else self.cfg.tick)
        finally:
            self.desk.stop.set()
            self._finish_threads()
            self._release_all("the task system shut down")
            self.store.clear_scheduler(os.getpid())

    def announce(self) -> None:
        """The scheduler announces nothing of itself: it is no machine (tasking/events.py). It
        announces the parts of machines it implements, each as an instance that says what it listens
        for and what it is: dispatch, an instance of the coordinator (no package: the coordinator is
        whoever fills that role, and Orchestra keeps each instance's card apart, so this erases
        nothing an agent coordinator said of itself), and in code mode the gate."""
        stacked = self.cfg.stacked              # the gate restacks when a parent moves or lands; a conflict is rework to offer
        self.events.publish(MACHINE_STARTED, {"mode": "continuous", "kind": "program", "log": str(LOG),
                                              "listens_for": DISPATCH_LISTENS + ([STACK_CONFLICT] if stacked else [])}, source=DISPATCH)
        if self.cfg.work == "code":
            self.gate_events.publish(MACHINE_STARTED, {"package": "tasks", "mode": "continuous", "kind": "program", "log": str(LOG),
                                                       "listens_for": [APPROVED] + ([REVIEW_REQUESTED, "gate.landed", "github.pr.merged"]
                                                                                    if stacked else [])})

    def _busy(self) -> bool:
        return bool(self.store.assignments()) or bool(self.gate_thread and self.gate_thread.is_alive())

    def _finish_threads(self) -> None:
        """A gate run lands or fails before the shutdown releases anything, so a ticket the gate
        is landing is never sent back under it."""
        if self.gate_thread and self.gate_thread.is_alive():
            log("waiting for the gate run to finish")
            self.gate_thread.join(timeout=self.cfg.gate_timeout + self.cfg.build_timeout + self.cfg.acceptance_ready_timeout)

    def tick(self) -> None:
        self.project.reload_config()
        self.cfg = self.project.config
        self._reap()
        self._service_kills()
        self._follow()
        if self.cfg.external_review:
            self.external.follow()
        try:
            self.recurring.tick()                     # an occurrence due now is made before the queue is read: offered this tick
        except Exception as e:  # noqa: BLE001 - a broken recurring definition never stops the scheduler
            log(f"recurring: {e!r}")
        self.stacks.tick()
        tickets = self.store.tickets_by_id()
        milestones = self.store.milestones_by_id()
        cap = self.store.capacity()
        eff = effective_priorities(tickets, self.cfg.aging_days_per_level)
        ordered = sorted(tickets.values(), key=lambda t: (eff.get(t.id, {}).get("effective", t.priority), t.created_at, t.id))
        paused = self.store.pickup_paused()
        out = self.store.assignments().values()
        busy = {r: sum(1 for a in out if a["role"] == r) for r in ("worker", "reviewer")}
        taken = {a["ticket"] for a in out}
        resting = self._resting(tickets)
        gate = self.cfg.work == "code" and self.gate_thread is None and not self.store.pipeline().get("paused")
        if gate and self.cfg.reuse_worktrees:       # its worktree claimed before any offer's: landing comes first
            self._start_gate(ordered)
        # review before pick: finished work lands before new work starts
        for t in ordered:
            if t.state == State.READY_FOR_REVIEW and busy["reviewer"] < cap["reviewers"] and t.id not in taken \
                    and (t.id, "reviewer") not in resting and self._assign(t, "reviewer"):
                busy["reviewer"] += 1
        for t in ordered:
            if paused or busy["worker"] >= cap["workers"]:
                break
            if t.id in taken or (t.id, "worker") in resting:
                continue
            if (t.state == State.REJECTED or (t.state == State.OPEN and self._ready(t, tickets, milestones))) \
                    and self._assign(t, "worker"):
                busy["worker"] += 1
        if gate and not self.cfg.reuse_worktrees:
            self._start_gate(ordered)
        for t in ordered:
            if self.cfg.work == "code" and t.state == State.CANCELLED and self.store.worktree(t.id):
                self._drop_worktree(t)
        self._measure_pool()
        self.staging.ensure_running()
        try:
            self.prober.tick(config_probes(self.project))
        except Exception as e:  # noqa: BLE001 - a broken probe never stops the scheduler
            log(f"probes: {e!r}")

    def _ready(self, t: Ticket, tickets: dict, milestones: dict) -> bool:
        """Ready to offer: its dependencies landed, or with stacking, all but the one it stacks on."""
        return self.stacks.ready(t, tickets, milestones) if self.cfg.stacked else is_ready(t, tickets, milestones)

    def _start_gate(self, ordered: list[Ticket]) -> None:
        """The longest-approved ticket to the gate. With reused worktrees the gate holds one of
        the pool's, claimed here before any offer is made, so landing comes before new work; with
        the pool full it waits like any other turn."""
        approved = sorted((t for t in ordered if t.state == State.APPROVED), key=lambda t: t.state_since)
        if self.cfg.external_review:                 # nothing lands here: each is handed off for its pull request
            for t in approved:
                try:
                    self.external.hand_off(t)
                except (Conflict, StoreError, TransitionError) as e:
                    log(f"gate: {t.id}: cannot hand it off: {e}")
            return
        approved = [t for t in approved if self.stacks.may_land(t)]     # with stacking, the bottom of a stack lands first
        if not approved:
            return
        tid, slot = approved[0].id, None
        if self.cfg.reuse_worktrees:
            try:
                slot = self.pool.claim(f"gate:{tid}", tid, prefer=tid)
            except GitError as e:
                log(f"gate: {tid}: no worktree from the pool: {e}")
                return
            if not self._pool_got(slot, tid, "gate"):
                return
        self.gate_thread = threading.Thread(target=self._gate, args=(tid, slot), daemon=True)
        self.gate_thread.start()

    def _pool_got(self, path: Path | None, tid: str, who: str) -> bool:
        """Whether the pool gave a worktree; when it did not, the turn waits, said once in the log
        until it gets one (the series ``tasks_worktrees`` shows how many wait)."""
        if path is not None:
            self.pool_waiting.discard((tid, who))
            return True
        if (tid, who) not in self.pool_waiting:
            self.pool_waiting.add((tid, who))
            log(f"{tid}: the worktree pool is full ({self.cfg.worktree_pool} in use); the {who} waits for a free one")
        return False

    def _measure_pool(self) -> None:
        """The turns waiting for a worktree of the pool, kept in the record for
        ``tasks_worktrees{state="waiting"}`` (tasking/measures.py; the pool's own record has the rest)."""
        if not (self.cfg.reuse_worktrees and self.cfg.work == "code"):
            return
        self.pool_waiting = {k for k in self.pool_waiting if self._still_waiting(*k)}
        self.store.set_pool_waiting(len(self.pool_waiting))

    def _still_waiting(self, tid: str, who: str) -> bool:
        try:
            state = self.store.get_ticket(tid).state
        except StoreError:
            return False
        return state in ({State.APPROVED} if who == "gate" else OFFERED_FROM[who])

    def _resting(self, tickets: dict[str, Ticket]) -> set[tuple[str, str]]:
        """The (ticket, role) offers that nobody took and that wait out their pause before going
        out again. One whose ticket changed since (a planner rewrote it) goes out at once; one
        whose ticket moved on is forgotten."""
        out, now_t = set(), time.time()
        for rec in self.store.untaken().values():
            t = tickets.get(rec["ticket"])
            if t is None or t.state not in OFFERED_FROM[rec["role"]]:
                self.store.set_untaken(rec["ticket"], rec["role"], None)
            elif rec["again_at"] > now_t and rec.get("version") == t.version:
                out.add((t.id, rec["role"]))
        return out

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
        if self.gate_thread and not self.gate_thread.is_alive():
            self.gate_thread = None

    def _worktree(self, t: Ticket, aid: str) -> tuple[Path | None, str]:
        """The worker's worktree on the ticket's branch: its own (made once, kept until it lands),
        or with reused worktrees one of the pool's reset for it (None: the pool is full, or the
        worktree's ``[tasks] worktree_setup`` failed and waits to be tried again)."""
        branch = t.branch or f"orchestra/{t.id}-{t.slug}"
        self.stacks.prepare(t, branch)               # with stacking: a new branch from its parent's, tracked by git-spice
        if self.cfg.reuse_worktrees:
            return self.pool.claim_for(aid, t.id, branch), branch
        path = self.project.worktrees_dir / t.id
        if not path.exists() or self.git.worktree_branch(path) != branch:
            self.git.worktree_remove(path)
            self.git.worktree_add(branch, path, self.cfg.base_branch)
        self.store.set_worktree(t.id, str(path), branch)
        try:
            self.setup.ensure(path)                  # [tasks] worktree_setup, once: never worked in unprepared
        except SetupError:
            return None, branch                      # said once per failure; the ticket waits for the retry
        return path, branch

    def _workspace(self, t: Ticket) -> Path:
        """A task's scratch folder (work = "tasks"): the worker's working directory, kept for the
        reviewer. Not a git checkout; nothing in it is merged anywhere."""
        path = self.project.workspaces_dir / t.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _assign(self, t: Ticket, role: str, *, type: str = ASSIGNED, source: str | None = None, cause: str | None = None,
                extra: dict | None = None) -> bool:
        """Offer a waiting ticket to a role (the queue): get its place ready (a folder, or a branch
        in code mode), record the assignment with its lease, and say so on the bus. Whatever
        serves the role takes it; the ticket moves (IN_PROGRESS, IN_REVIEW) and gets its owner
        when one claims it (``_follow``), so an offer nobody takes changes nothing. Rejected work
        is offered first to the worker that did it (``to``), for the return timeout, unless it
        already waited for that worker since it was rejected. Published as dispatch
        (``coordinator/dispatch``, the queue), or for external review as ``source`` with ``type``
        ``changes.requested``, ``cause`` and ``extra`` (``_offer_back``)."""
        tasks = self.cfg.work != "code"
        aid = new_id(t.id, role)
        try:
            if role == "worker":
                path, branch = (self._workspace(t), None) if tasks else self._worktree(t, aid)
                if path is None:
                    return self._pool_got(None, t.id, role) if self.cfg.reuse_worktrees else False
            else:
                if tasks:
                    path, branch = self.project.workspaces_dir / t.id, None
                elif self.cfg.reuse_worktrees:
                    # the work is on the branch: a reviewer gets any worktree reset to it
                    if not (t.branch and self.git.branch_exists(t.branch)):
                        self.reports.reject(t.id, SCHEDULER, "the ticket's branch is missing at review time; "
                                            "the work has to be done again", counted=False)
                        return False
                    path, branch = self.pool.claim_for(aid, t.id, t.branch), t.branch
                    if path is None:
                        return self._pool_got(None, t.id, role)
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
        if self.cfg.reuse_worktrees and not tasks:
            self._pool_got(path, t.id, role)
        untaken = self.store.untaken().get(f"{t.id}/{role}") or {}
        to = t.worker if role == "worker" and t.state == State.REJECTED and t.worker and untaken.get("returned") != t.version else None
        lease = self.cfg.return_timeout if to else LEASE_S
        turns = {"worker_turns": t.worker_turns + 1, "branch": branch} if role == "worker" else {}   # as the ticket will read once accepted
        base = None if tasks else self.stacks.base(self.store.get_ticket(t.id))     # with stacking, a parent's branch
        offer = {"assignment": aid, "role": role, "ticket": t.id, "workspace": str(path), "branch": branch,
                 "base": base, "lease_s": LEASE_S, "brief": {**brief(t), **turns}}
        if to:
            offer["to"] = to
        if not tasks and self.cfg.unit:
            offer["unit_command"] = self.cfg.unit[0].command.replace("{reports}", str(self.project.logs_dir / "worker-reports" / t.id))
        if extra:
            offer.update(extra)
        if role == "reviewer":                       # what the worker said and ran when it asked for the review, as recorded
            offer["review"] = {"summary": self._worker_summary(t.id), "commit": None if tasks else t.reviewed_commit,
                               "test_output": None if tasks else t.test_output, "worker": t.worker}
        record = {"id": aid, "ticket": t.id, "role": role, "workspace": str(path), "branch": branch, "base": base, "to": to,
                  "assigned_at": time.time(), "lease_until": time.time() + lease, "instance": None, "accepted_at": None,
                  "offered_since": untaken.get("since") or time.time()}   # the first of the offers nobody took
        if source:
            record.update(by=source, cause=cause)
        self.store.put_assignment(record)
        # dispatch offers it (the coordinator's); or the pull request's reviewer, whose side it stands in for
        self.events.publish(type, offer, source=source or DISPATCH, subject=t.id, **({"causationid": cause} if cause else {}))
        log(f"{t.id}: {'assigned' if type == ASSIGNED else type + ' from ' + str(source) + ','} to "
            + (f"{to} (the worker that did it)" if to else f"a {role}") + f" ({aid})")
        return True

    def _offer_back(self, t: Ticket, source: str, cause: str | None, fields: dict) -> bool:
        """External review: the feedback on a pull request sends the work back to the worker that
        did it, as ``changes.requested`` from whoever gave the feedback (tasking/external.py)."""
        return self._assign(t, "worker", type=CHANGES_REQUESTED, source=source, cause=cause, extra=fields)

    def _withdraw(self, tid: str, reason: str) -> None:
        """Whoever holds the ticket, or is offered it, is released and the ticket goes back to
        waiting, without counting against anyone (as the operator's stop): its pull request was
        merged meanwhile (tasking/external.py)."""
        for a in self.store.assignments().values():
            if a["ticket"] == tid:
                log(f"{tid}: the {a['role']}'s turn withdrawn: {reason}")
                self._release(a, reason)
                if a.get("instance") is not None:
                    self._settle(a, lambda a=a: self.reports.back_to_waiting(tid, f"{reason} during the {a['role']}'s turn"))

    def _worker_summary(self, tid: str) -> str:
        return next((n.get("text", "") for n in reversed(self.store.notes(tid)) if n.get("author") == "worker"), "")

    def _follow(self) -> None:
        """What the machines said since the last tick, recorded: the first valid claim of an
        offer names its holder and moves the ticket, a heartbeat extends the lease, and the event
        that ends a turn is applied to the ticket: a review requested or changes requested (the
        next turn is recorded as offered, by the machine that offered it), an approval, a report.
        Anything from a machine that does not hold the turn is ignored. Then every lease that ran
        out is released: a holder that went quiet counts as a crash; an offer nobody took is
        dropped and offered again from the queue (``_untaken``)."""
        out = self.store.assignments()
        evs, self.offset = self.events.after(self.offset)
        for ev in evs:
            try:
                kind = ev.get("type")
                if kind == MACHINE_STARTED and ASSIGNED in (ev.get("listens_for") or []) and self.store.untaken():
                    self.store.offer_untaken_now()
                    log(f"{ev.get('source')} started and listens for {ASSIGNED}: the offers nobody took go out again now")
                    continue
                if kind not in FOLLOWED:
                    continue
                aid = ev.get("ends") if kind in ENDS else ev.get("assignment")
                if kind == CHANGES_REQUESTED and aid is None and ev.get("pull"):
                    continue                          # external review's own offer, recorded when it was made
                if aid not in out:
                    self._not_held(ev, aid)
                    continue
                a = out[aid]
                if kind == ACCEPTED:
                    self._claim(a, ev, out)
                    continue
                if ev.get("source") != a.get("instance"):
                    continue
                if kind == WORKING:
                    said = {"text": ev["text"][-200:]} if isinstance(ev.get("text"), str) else {}
                    self.store.update_assignment(aid, lease_until=time.time() + LEASE_S, **said)
                    continue
                out.pop(aid)
                nxt = self._end(a, ev)
                if nxt is not None:
                    out[nxt["id"]] = nxt
            except Exception as e:  # noqa: BLE001 - one bad event never loses the others
                log(f"{ev.get('ticket')}: cannot apply {ev.get('type')} from {ev.get('source')}: {e!r}")
        now_t = time.time()
        for aid, a in self.store.assignments().items():
            if a.get("instance") is None:
                t = self.store.get_ticket(a["ticket"])
                if a["lease_until"] < now_t:
                    self._untaken(a, t, now_t)
                elif t.state not in OFFERED_FROM[a["role"]]:                              # cancelled meanwhile
                    self._release(a, f"the ticket is {t.state}")
                    self.store.set_untaken(a["ticket"], a["role"], None)
            elif a["lease_until"] < now_t:
                self._release(a, f"no word from {a['instance']} for {int(LEASE_S)} s")
                log(f"{a['ticket']}: {a['role']} assignment {aid} released: the lease ran out")
                self._settle(a, lambda a=a: self.reports.apply(a, None, "", True))

    def _not_held(self, ev: dict, aid: str | None) -> None:
        """An event for a turn this scheduler does not hold: released (a restart, a lease that ran
        out, the operator), settled already, or never valid. A claim of it is answered, or the
        instance would wait out its lease for a word, unless the turn was lately settled: then
        another claim was first, and the claimant read that on the bus."""
        kind = ev.get("type")
        if kind != WORKING:                       # a heartbeat for one is not worth a line
            log(f"{ev.get('ticket')}: {kind} from {ev.get('source')} for assignment {aid} ignored: not held "
                "(released, already settled, or never valid)")
        if kind == ACCEPTED and aid and ev.get("source") and aid not in self.settled:
            role = ev.get("role") if ev.get("role") in OFFERED_FROM else machine_and_instance(ev["source"])[0]
            self.events.publish(RELEASED, {"assignment": aid, "ticket": ev.get("ticket"), "role": ev.get("role"),
                                           "reason": "not held (released, already settled, or never valid)",
                                           "instance": ev["source"]}, source=role, subject=ev.get("ticket"))

    def _claim(self, a: dict, ev: dict, out: dict) -> None:
        """A claim of a held offer. The first valid one holds it (``_accept``); a later one lost
        the race and knows it from the bus, so nothing is said to it. A claim of an offer
        addressed to another instance is not valid and changes nothing."""
        if a.get("instance") is not None:
            if a["instance"] != ev.get("source"):
                log(f"{a['ticket']}: {ev.get('source')} claimed {a['id']} after {a['instance']}, which holds it")
            return
        if a.get("to") and ev.get("source") != a["to"]:
            log(f"{a['ticket']}: {ev.get('source')} claimed {a['id']}, which is offered to {a['to']} only; ignored")
            return
        if self._accept(a, ev):
            out[a["id"]] = self.store.assignments().get(a["id"], a)
        else:
            out.pop(a["id"], None)

    def _end(self, a: dict, ev: dict) -> dict | None:
        """The holder ended its turn: what it said is applied, and the assignment is dropped; when
        the work passed on to another machine (a review requested, changes requested), the turn
        that event offers is recorded in its place and returned."""
        kind = ev["type"]
        self.settled.append(a["id"])              # its holder ended it: a later claim of it lost, and knows
        if kind == REVIEW_REQUESTED and a["role"] == "worker":
            return self._hand_over(a, ev, "reviewer", lambda: self.reports.review_requested(a, ev))
        if kind == CHANGES_REQUESTED and a["role"] == "reviewer":
            return self._hand_over(a, ev, "worker", lambda: self.reports.verdict(a, ev))
        if kind == APPROVED and a["role"] == "reviewer":
            self._settle(a, lambda: self.reports.verdict(a, ev))
        elif kind == REPORTED:
            self._settle(a, lambda: self.reports.apply(a, ev.get("report"), ev.get("text") or "", bool(ev.get("failed")), cause=ev))
        else:
            self._settle(a, lambda: self.reports.apply(a, None, f"{kind} does not end a {a['role']}'s turn", True))
        return None

    def _hand_over(self, a: dict, ev: dict, role: str, record: Callable[[], bool]) -> dict | None:
        """The work passed on: the step is recorded (``record``), then the turn the event offers
        stands in the record in place of the one it ended, with the same workspace (and a pool
        worktree passes with it), for the machine that offered it to have taken. An offer the
        record cannot stand behind (another ticket or role, another workspace, no id, rejected
        work addressed to nobody) is not recorded: the ticket waits in the queue instead, and a
        claim of it is released."""
        nxt = None
        try:
            if not record():
                return None
            why = self._unfit(a, ev, role)
            if why:
                log(f"{a['ticket']}: the {ev['type']} from {ev.get('source')} is recorded, but its offer is not: {why}; "
                    "the ticket waits in the queue")
                return None
            to = ev.get("to") if role == "worker" else None
            lease = self.cfg.return_timeout if to else LEASE_S
            nxt = {"id": ev["assignment"], "ticket": a["ticket"], "role": role, "workspace": a.get("workspace"), "branch": a.get("branch"),
                   "base": a.get("base"),
                   "to": to, "by": ev.get("source"), "cause": ev.get("id"), "assigned_at": time.time(), "lease_until": time.time() + lease,
                   "instance": None, "accepted_at": None, "offered_since": time.time()}
            self.store.hand_over(a["id"], nxt)
            log(f"{a['ticket']}: {ev.get('source')} offered the {role}'s turn {nxt['id']}" + (f" to {to}" if to else ""))
            return nxt
        except Exception as e:  # noqa: BLE001 - the ticket must not stay active without a holder
            log(f"{a['ticket']}: cannot apply the {a['role']}'s {ev['type']}: {e!r}")
            self.reports.back_to_waiting(a["ticket"], f"the task system could not apply the {a['role']}'s {ev['type']} ({e})")
            return None
        finally:
            if nxt is None:
                self.store.drop_assignment(a["id"])

    @staticmethod
    def _unfit(a: dict, ev: dict, role: str) -> str | None:
        """Why the offer an event that ends a turn makes cannot be recorded, or None."""
        if not isinstance(ev.get("assignment"), str) or not ev["assignment"]:
            return "it names no assignment"
        if ev.get("ticket") != a["ticket"] or ev.get("role") != role:
            return f"it offers {ev.get('ticket')}'s {ev.get('role')} turn, not {a['ticket']}'s {role}"
        if ev.get("workspace") != a.get("workspace"):
            return f"its workspace {ev.get('workspace')} is not the turn's {a.get('workspace')}"
        if role == "worker" and not ev.get("to"):
            return "rejected work is addressed to the worker that did it, and it names none"
        return None

    def _untaken(self, a: dict, t: Ticket, now_t: float) -> None:
        """An offer nobody took within its lease: withdrawn, and made again after a pause that
        doubles with each offer nobody took (``AGAIN_FIRST_S`` up to ``AGAIN_MAX_S``), so a ticket
        no machine serving the role will take neither holds a slot for ever nor fills the bus. It
        changed nothing: no turn, no note, no transition. The record keeps when it was first
        offered, for the Tasks panel's "offered, not taken". A ticket rewritten since the last of
        them starts over: its first offer is this one, and none were missed before it."""
        if a.get("to"):
            self._not_returned(a, t, now_t)
            return
        rec = self.store.untaken().get(f"{a['ticket']}/{a['role']}")
        if rec is None or rec.get("version") != t.version:
            rec = {"ticket": a["ticket"], "role": a["role"], "misses": 0,
                   "since": a["assigned_at"] if rec else a.get("offered_since") or a["assigned_at"]}
        misses = rec["misses"] + 1
        pause = min(AGAIN_FIRST_S * 2 ** (misses - 1), AGAIN_MAX_S)
        self._release(a, f"nobody filled the role {a['role']} within {int(LEASE_S)} s")
        self.store.set_untaken(a["ticket"], a["role"], {**rec, "misses": misses, "version": t.version, "again_at": now_t + pause})
        log(f"{a['ticket']}: nobody took the {a['role']}'s offer {a['id']} within {int(LEASE_S)} s ({misses} offer(s) not taken); "
            f"offered again in {int(pause)} s, or when the ticket changes or a machine that listens for {ASSIGNED} starts")

    def _not_returned(self, a: dict, t: Ticket, now_t: float) -> None:
        """Rejected work addressed to the worker that did it, which did not take it within the
        return timeout (gone, or busy): taken back, said so on the ticket, and offered to any
        worker at once."""
        wait = int(self.cfg.return_timeout)
        why = f"{a['to']} did not take the rejected work back within {wait} s (gone, or busy)"
        self._release(a, f"{why}; offered to any worker")
        self.store.add_note(t.id, Note(author=SCHEDULER, text=f"{why}; back in the queue for any worker"))
        rec = self.store.untaken().get(f"{a['ticket']}/{a['role']}") or {"ticket": a["ticket"], "role": a["role"], "misses": 0, "since": now_t}
        t = self.store.get_ticket(t.id)
        self.store.set_untaken(a["ticket"], a["role"], {**rec, "version": t.version, "returned": t.version, "again_at": 0.0})
        log(f"{a['ticket']}: {why}; offered to any worker")

    def _accept(self, a: dict, ev: dict) -> bool:
        """The first acceptance of an offer: the holder is the event's source (an ``instance``
        that says otherwise is refused and the offer stays open), and the ticket moves to the
        role's active state with that owner. A ticket that can no longer move (cancelled
        meanwhile) is released to the instance and the offer dropped. True while the offer stands."""
        holder, claimed = ev["source"], ev.get("instance")
        if claimed is not None and claimed != holder:
            # told under the source, the machine that sent it: one that filters releases by its own name hears it
            self._release(a, f"the acceptance claimed instance {claimed}, but an instance must be the event's source ({holder})",
                          instance=holder)
            return True
        t = self.store.get_ticket(a["ticket"])
        fields = ({"worker_turns": t.worker_turns + 1, "worker": holder, **({"branch": a["branch"]} if a.get("branch") else {})}
                  if a["role"] == "worker" else {})
        try:
            self.store.transition(t.id, t.version, State.IN_PROGRESS if a["role"] == "worker" else State.IN_REVIEW, by=machine_and_instance(holder)[0] or "unknown",
                                  cause=ev, owner=holder, assignment=a["id"], **fields)
        except (Conflict, StoreError, TransitionError) as e:
            self._release(a, f"the ticket cannot be {'worked' if a['role'] == 'worker' else 'reviewed'} now: {e}", instance=holder)
            self.store.drop_assignment(a["id"])
            log(f"{t.id}: {holder} accepted {a['id']} but {e}; released")
            return False
        self.store.update_assignment(a["id"], instance=holder, accepted_at=time.time(), lease_until=time.time() + LEASE_S)
        self.store.set_untaken(a["ticket"], a["role"], None)
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

    def _gate(self, ticket_id: str, slot: Path | None = None) -> None:
        """A gate run, as the gate (``merge_gate``): it takes the approval (the ticket's
        ``caused_by``) and acts on the ticket itself."""
        try:
            t = self.store.get_ticket(ticket_id)
            self.gate_store.transition(ticket_id, t.version, State.MERGING, because=t.caused_by)
            log(f"gate: {ticket_id}")
            run = run_gate(self.project, self.gate_store, self.gate_events, ticket_id, slot=slot, because=t.caused_by)
            log(f"gate {run['id']}: {run['status']} {run.get('summary', '')}")
        except (Conflict, StoreError, TransitionError) as e:
            log(f"gate: {e}")
        finally:
            if slot is not None:
                self.store.release_slot(f"gate:{ticket_id}")

    def _drop_worktree(self, t: Ticket) -> None:
        wt = self.store.worktree(t.id)
        if wt:
            self.git.worktree_remove(Path(wt["path"]))
            self.store.remove_worktree(t.id)
