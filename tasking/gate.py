"""The merge gate: assemble, build, unit, start, acceptance, land. SPEC.md section 5. The gate
lands one ticket per run, so a failure is attributed to that ticket unless the base branch itself
fails."""

from __future__ import annotations

import contextlib
import glob
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .appends import CONFIG, resolve_appends
from .config import Project, TestSpec
from .events import COORDINATOR, SUPERVISOR, Events
from .git import Git, GitError
from .model import Note, State, now
from .pool import Pool
from .reports import send_back
from .staging import Staging, build_python
from .store import LocalStore


def run_specs(specs: list[TestSpec], cwd: Path, log_dir: Path, placeholders: dict[str, str] | None = None,
              stage: str = "unit") -> tuple[bool, list[dict]]:
    results = []
    ok = True
    for spec in specs:
        cmd = spec.command.format(**(placeholders or {}))
        t0 = time.time()
        try:
            p = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=spec.timeout, check=False)
            rc, out = p.returncode, (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        except subprocess.TimeoutExpired as e:
            rc, out = 124, f"timed out after {spec.timeout}s\n{(e.stdout or b'')[-4000:]!r}"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{stage}-{spec.name}.log"
        log_path.write_text(out, encoding="utf-8")
        passed = rc == 0 or rc == 5  # pytest 5: no tests collected
        ok = ok and passed
        results.append({"stage": stage, "name": spec.name, "command": cmd, "status": "passed" if passed else "failed",
                        "returncode": rc, "duration": round(time.time() - t0, 1), "log": str(log_path),
                        "tail": out[-3000:]})
        if not passed:
            break
    return ok, results


def build(project: Project, cwd: Path, log_dir: Path, into: str) -> tuple[list[dict], str | None]:
    """The ``[build]`` stage in ``cwd``: the command run, then the artifact it names matched and
    copied to ``artifacts/<into>/``. Returns the stage results and the artifact's path (None when
    the stage failed, or no artifact is configured)."""
    cfg = project.config
    ok, res = run_specs([TestSpec("build", cfg.build_command, timeout=cfg.build_timeout)], cwd, log_dir, stage="build")
    if not ok or not cfg.build_artifact:
        return res, None
    matches = sorted(glob.glob(str(cwd / cfg.build_artifact)))
    if not matches:
        return [*res, {"stage": "build", "name": "artifact", "status": "failed", "tail": f"no artifact matched {cfg.build_artifact}"}], None
    dest = project.artifacts_dir / into
    dest.mkdir(parents=True, exist_ok=True)
    return res, str(shutil.copy(matches[-1], dest))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status < 500:
                    return True
        except (OSError, urllib.error.URLError):     # not listening yet, or refusing: try again
            time.sleep(1)
            continue
        time.sleep(1)
    return False


class _Target:
    """The built artifact running for acceptance tests."""

    def __init__(self, project: Project, cwd: Path, artifact: str | None, log_dir: Path):
        cfg = project.config
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        try:
            python = build_python(project, artifact)
        except subprocess.CalledProcessError:
            python = sys.executable
        self.placeholders = {"artifact": artifact or "", "port": str(self.port), "url": self.url, "python": python}
        cmd = (cfg.acceptance_start or "").format(**self.placeholders)
        self.log = (log_dir / "target.log").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(cmd, shell=True, cwd=str(cwd), stdout=self.log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        ready = (cfg.acceptance_ready or "").format(**self.placeholders)
        if ready.startswith("http"):
            self.ready = _wait_ready(ready, cfg.acceptance_ready_timeout)
        else:
            time.sleep(min(cfg.acceptance_ready_timeout, 5))
            self.ready = self.proc.poll() is None

    def stop(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            with contextlib.suppress(ProcessLookupError):    # gone meanwhile: nothing to kill
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.log.close()


def _stages(project: Project, cwd: Path, log_dir: Path, run: dict) -> bool:
    """Run build, unit, start+acceptance in cwd. Appends stage results to run['stages']."""
    cfg = project.config
    artifact = None
    log_dir.mkdir(parents=True, exist_ok=True)       # the target's log goes here even when no spec ran before it
    if cfg.build_command:
        res, artifact = build(project, cwd, log_dir, run["id"])
        run["stages"] += res
        if any(s["status"] == "failed" for s in res):
            return False
        if artifact:
            run["artifact"] = artifact
    ok, res = run_specs(cfg.unit, cwd, log_dir, stage="unit")
    run["stages"] += res
    if not ok:
        return False
    if cfg.acceptance and cfg.acceptance_start:
        target = _Target(project, cwd, artifact, log_dir)
        try:
            if not target.ready:
                run["stages"].append({"stage": "start", "name": "target", "status": "failed",
                                      "tail": (log_dir / "target.log").read_text()[-3000:]})
                return False
            ok, res = run_specs(cfg.acceptance, cwd, log_dir, target.placeholders, stage="acceptance")
            run["stages"] += res
            return ok
        finally:
            target.stop()
    return True


def run_gate(project: Project, store: LocalStore, events: Events, ticket_id: str, slot: Path | None = None,
             because: str | None = None) -> dict:
    """One gate run for ``ticket_id``. Its worktrees are made for the run and removed after it, or,
    with ``slot`` (a worktree of the pool the scheduler claimed for it: tasking/pool.py), that one
    worktree is reset for the run and, if the base needs checking alone, again for that. The
    scheduler runs it with a store and events of the gate's own source (``merge_gate``): the gate
    is a machine of the task system's, acting on the approval ``because`` names."""
    cfg = project.config
    git = Git(project.repo_root)
    pool = Pool(project, store, git) if slot is not None else None

    def make(branch: str, where: Path) -> None:
        if pool:
            pool.reset(where, branch)
        else:
            git.worktree_add(branch, where, base)

    def drop(branch: str, where: Path) -> None:
        if pool:
            pool.detach(where)
        else:
            git.worktree_remove(where)
        git.branch_delete(branch)

    run_id = uuid.uuid4().hex[:10]
    base = cfg.base_branch
    run: dict = {"id": run_id, "ticket": ticket_id, "base_branch": base, "status": "running", "stages": [],
                 "started_at": now(), "finished_at": None, "summary": "", "conflict": False}
    store.save_gate_run(run)
    events.emit("gate.started", because=because, gate_run=run_id, ticket=ticket_id)
    gate_branch = f"orchestra/gate-{run_id}"
    path = slot or project.gate_dir / run_id
    log_dir = project.logs_dir / run_id

    def reject(reason: str, conflict: bool = False) -> None:
        t = store.get_ticket(ticket_id)
        if t.state != State.MERGING:
            return
        store.add_note(ticket_id, Note(author="merge_gate", kind="gate", text=reason[-4000:]))
        # a merge conflict is routine: the branch simply needs the base merged in. It goes straight
        # back to the ticket's worker and does not count as a review round, so it never triggers
        # the repeated-rejection escalation; a failed stage counts, and escalates like a reviewer's
        send_back(store, events, cfg.review_rounds_before_escalation, t, "merge_gate", reason[-8000:], counted=not conflict,
                  gate_rejections=t.gate_rejections + 1)
        run["conflict"] = conflict

    def error(what: str) -> dict:
        """The run could not finish: nothing is attributed to the ticket, which waits for the
        gate again once the pipeline resumes; the supervisor role is told."""
        run["status"] = "error"
        run["summary"] = what
        t = store.get_ticket(ticket_id)
        if t.state == State.MERGING:
            store.transition(ticket_id, t.version, State.APPROVED)
        store.set_pipeline(True, f"gate run {run_id} error: {what}")
        events.attention(SUPERVISOR, f"gate run {run_id} hit an error and the pipeline is paused: {what}; "
                         "it resumes on pipeline.resume_requested")
        events.emit("gate.error", gate_run=run_id, error=what)
        return run

    try:
        base_commit = git.rev_parse(base)
        run["base_commit"] = base_commit
        make(gate_branch, path)
        t = store.get_ticket(ticket_id)
        p = git.merge_no_ff(t.reviewed_commit or t.branch or "", path, f"Merge {ticket_id}: {t.title}")
        if p.returncode != 0 and resolve_appends(git, path):
            run["resolved"] = f"{CONFIG}: both sides appended tables at the same place; both kept, {base}'s first"
        elif p.returncode != 0:
            git.abort_merge(path)
            reject(f"The branch conflicts with `{base}`: another ticket landed first, so `{base}` has to be merged "
                   f"into it before it can land (git output below). The ticket is offered to its worker again.\n\n{p.stdout}\n{p.stderr}",
                   conflict=True)
            run["status"] = "conflict"
            run["summary"] = f"{ticket_id} conflicted with {base}; sent back to its worker to merge {base}"
            events.emit("gate.conflict", gate_run=run_id, ticket=ticket_id)
            return run

        ok = _stages(project, path, log_dir, run)
        if ok:
            gate_commit = git.rev_parse("HEAD", cwd=path)
            run["gate_commit"] = gate_commit
            followed = git.land(base, gate_commit, base_commit)
            unseen = None if followed or "orchestra.toml" not in git.changed(base_commit, gate_commit) else (
                f"{ticket_id} changed orchestra.toml, but the repository root is on {git.current_branch()}, not {base}, so "
                f"only the {base} ref moved and the root's orchestra.toml is as it was: the change (a new [[machines]] "
                f"entry, say) takes effect only when the root is updated (check out {base} there, or merge it in)")
            if cfg.push:
                git.run("push", "origin", base, check=False)
            t = store.get_ticket(ticket_id)
            store.transition(ticket_id, t.version, State.DONE)
            store.add_note(ticket_id, Note(author="merge_gate", kind="gate", text=f"landed on {base} as {gate_commit[:10]}"))
            wt = store.worktree(ticket_id)
            if wt:
                git.worktree_remove(Path(wt["path"]))
                store.remove_worktree(ticket_id)
            if t.branch:
                if pool:
                    with contextlib.suppress(GitError):      # a reused worktree that last served it may still have it
                        pool.release_branch(t.branch)
                git.branch_delete(t.branch)
            run["status"] = "landed"
            run["summary"] = (f"landed {ticket_id} on {base} at {gate_commit[:10]}" + (f" ({run['resolved']})" if run.get("resolved") else "")
                              + (f"; {unseen}" if unseen else ""))
            events.emit("gate.landed", gate_run=run_id, ticket=ticket_id, commit=gate_commit)
            if unseen:
                events.attention(COORDINATOR, unseen, ticket=ticket_id)
            if run.get("artifact") and cfg.staging_configured:
                build_rec = {"id": run_id, "gate_run_id": run_id, "commit": gate_commit,
                             "artifact_path": run["artifact"], "created_at": now()}
                store.record_build(build_rec)
                run["staging"] = "deployed" if Staging(project, store, events).deploy(build_rec) else "failed"
            return run

        # failure: is the base itself broken?
        failed = [s for s in run["stages"] if s["status"] == "failed"]
        failure_text = "\n\n".join(f"[{s['stage']}:{s['name']}] exit {s.get('returncode')}\n{s.get('tail', '')}" for s in failed)
        base_path = path if pool else project.gate_dir / f"{run_id}-base"
        base_ok = True
        try:
            make(f"orchestra/gate-{run_id}-base", base_path)
            base_run = {"id": run_id, "stages": []}
            base_ok = _stages(project, base_path, log_dir / "base", base_run)
            run["base_check"] = base_run["stages"]
        finally:
            drop(f"orchestra/gate-{run_id}-base", base_path)
        if not base_ok:
            t = store.get_ticket(ticket_id)
            store.transition(ticket_id, t.version, State.APPROVED)
            run["status"] = "base_broken"
            run["summary"] = f"{base} fails the gate with nothing merged; pipeline paused"
            store.set_pipeline(True, f"{base} fails the gate (run {run_id})")
            events.attention(SUPERVISOR, f"{base} is broken: gate run {run_id} failed with no branches applied")
            events.emit("gate.base_broken", gate_run=run_id)
            return run
        reject(f"Merge gate failed for {ticket_id} on top of {base} (run {run_id}):\n\n{failure_text}")
        run["status"] = "rejected"
        run["summary"] = f"rejected {ticket_id}: {', '.join(s['stage'] + ':' + s['name'] for s in failed)}"
        events.emit("gate.rejected", gate_run=run_id, ticket=ticket_id)
        return run
    except GitError as e:
        return error(str(e))
    except Exception as e:  # noqa: BLE001 - whatever failed, the ticket must not stay MERGING
        return error(repr(e))
    finally:
        drop(gate_branch, path)
        run["finished_at"] = now()
        store.save_gate_run(run)
