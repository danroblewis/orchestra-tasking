"""The AI agents' side of assignment: answers ``task.assigned`` for the roles it serves (worker,
reviewer) by running an agent session on the ticket, says it is still working while the turn
runs, and reports what the agent reported (tasking/assignments.py).

It runs in the scheduler's process for now and uses its session helpers (harnesses, prompts,
transcripts); it is the part that moves to orchestra-agents (docs/proposals/agents-split.md).
It knows the task system only through the bus and the ticket it is given; the scheduler applies
every transition."""

from __future__ import annotations

import threading
from pathlib import Path

from . import agents
from .assignments import ACCEPTED, ASSIGNED, HEARTBEAT_S, RELEASED, REPORTED, WORKING, Follower
from .gate import run_specs


class AgentRunner:
    ROLES = ("worker", "reviewer")

    def __init__(self, loop):
        self.loop = loop
        self.events = loop.events
        self.follow = Follower(loop.events)
        self.threads: dict[str, threading.Thread] = {}      # assignment -> the thread running it
        self.sessions: dict[str, str] = {}                   # assignment -> its session id

    def busy(self) -> bool:
        return any(th.is_alive() for th in self.threads.values())

    def tick(self) -> None:
        for aid in [a for a, th in self.threads.items() if not th.is_alive()]:
            self.threads.pop(aid)
        for ev in self.follow.new():
            if ev.get("type") == ASSIGNED and ev.get("role") in self.ROLES and ev.get("assignment") not in self.threads:
                th = threading.Thread(target=self._run, args=(ev,), daemon=True, name=ev["assignment"])
                self.threads[ev["assignment"]] = th
                th.start()
            elif ev.get("type") == RELEASED and ev.get("assignment") in self.sessions:
                proc = self.loop.live_procs.get(self.sessions[ev["assignment"]])
                if proc is not None:
                    try:
                        proc.kill()                           # the turn ends; its report says so
                    except OSError:
                        pass

    # -- one assignment --------------------------------------------------------

    def _run(self, a: dict) -> None:
        loop, store = self.loop, self.loop.store
        aid, role, tid = a["assignment"], a["role"], a["ticket"]
        path = Path(a["workspace"])
        sid = f"{role}-{tid}"
        instance = loop._instance(sid, role, tid)
        self.sessions[aid] = sid
        say = lambda kind, **data: self.events.publish(kind, {"assignment": aid, "ticket": tid, "role": role, **data},
                                                       source=instance, subject=tid)
        say(ACCEPTED, instance=instance, session=sid)
        done = threading.Event()

        def beat():
            while not done.wait(HEARTBEAT_S):
                say(WORKING)
        threading.Thread(target=beat, daemon=True, name=f"{aid}:beat").start()
        report, text, failed, commit = None, "", True, None
        try:
            t = store.get_ticket(tid)
            if role == "worker":
                task, nudge = self._work_task(t, a)
            else:
                task, nudge, commit = self._review_task(t, a, path)
            proc = loop._session(sid, role, path, tid)
            try:
                loop._session_running(sid, role, proc, tid)
                if loop._trim_if_large(proc, sid, role) == "rotated":
                    proc = loop._session(sid, role, path, tid)
                    loop._session_running(sid, role, proc, tid)
                report, text, failed = loop._prompt_with_retry(proc, tid, task, nudge)
            finally:
                proc.close()
                loop._session_idle(sid, text)
        except Exception as e:  # noqa: BLE001 - whatever happened, the scheduler hears how it ended
            text = f"{type(e).__name__}: {e}"
        finally:
            done.set()
            self.sessions.pop(aid, None)
            say(REPORTED, report=report, failed=failed, text=(text or "")[-2000:], commit=commit)

    def _work_task(self, t, a: dict) -> tuple[str, str]:
        loop = self.loop
        if a.get("branch") is None:                           # a task: a folder and a result
            return (agents.worker_task_tasks(t),
                    "You stopped without reporting. Call `report_ticket` now: READY_FOR_REVIEW if your result is written, "
                    "otherwise BLOCKED with the reason.")
        unit_cmd = loop.cfg.unit[0].command if loop.cfg.unit else None
        return (agents.worker_task(loop.project, t, unit_cmd),
                "You stopped without reporting. Call `report_ticket` now: READY_FOR_REVIEW if your work is "
                "committed and tested, otherwise BLOCKED with the reason.")

    def _review_task(self, t, a: dict, path: Path) -> tuple[str, str, str | None]:
        loop, store = self.loop, self.loop.store
        nudge = "You stopped without a verdict. Call `report_review` now with APPROVED or REJECTED."
        if a.get("branch") is None:                           # a task: the worker's result in its folder
            files = sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file())[:200]
            result = (path / "RESULT.md").read_text(encoding="utf-8", errors="replace") if (path / "RESULT.md").exists() else ""
            summary = next((n.get("text", "") for n in reversed(store.notes(t.id)) if n.get("author") == "worker"), "")
            return agents.reviewer_task_tasks(t, summary, files, result), nudge, None
        base, test_output = loop.cfg.base_branch, None        # code: the diff, and the unit tests run on it
        if loop.cfg.unit:
            ok, res = run_specs(loop.cfg.unit, path, loop.project.logs_dir / "review" / t.id, stage="unit")
            test_output = "\n".join(f"$ {r['command']}\n{r['tail']}\n[{r['status']}]" for r in res)
        commit = t.reviewed_commit or loop.git.rev_parse("HEAD", cwd=path)
        task = agents.reviewer_task(loop.project, t, commit, loop.git.diff(base, commit, cwd=path),
                                    loop.git.changed_files(base, commit, cwd=path), loop.git.log(base, commit, cwd=path), test_output)
        return task, nudge, commit
