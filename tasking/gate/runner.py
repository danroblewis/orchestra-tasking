"""The merge gate: assemble, build, unit, start, acceptance, land. SPEC.md section 5.

v1 gates one ticket per run (batch size 1), so a failure is attributed to that ticket unless
the base branch itself fails. Batching and bisection plug in here later."""

from __future__ import annotations

import contextlib
import glob
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ..config import Project, TestSpec
from ..events import Events
from ..git import Git, GitError
from ..model import Note, State, now
from ..staging import Staging, build_python, record_build
from ..store import LocalStore


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
            python = os.sys.executable
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
    if cfg.build_command:
        ok, res = run_specs([TestSpec("build", cfg.build_command, timeout=cfg.build_timeout)], cwd, log_dir, stage="build")
        run["stages"] += res
        if not ok:
            return False
        if cfg.build_artifact:
            matches = sorted(glob.glob(str(cwd / cfg.build_artifact)))
            if not matches:
                run["stages"].append({"stage": "build", "name": "artifact", "status": "failed",
                                      "tail": f"no artifact matched {cfg.build_artifact}"})
                return False
            dest = project.state_dir / "artifacts" / run["id"]
            dest.mkdir(parents=True, exist_ok=True)
            artifact = str(shutil.copy(matches[-1], dest))
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


def run_gate(project: Project, store: LocalStore, events: Events, ticket_ids: list[str]) -> dict:
    cfg = project.config
    git = Git(project.repo_root)
    run_id = uuid.uuid4().hex[:10]
    base = cfg.base_branch
    run: dict = {"id": run_id, "batch": list(ticket_ids), "base_branch": base, "status": "running",
                 "stages": [], "started_at": now(), "finished_at": None, "merged": [], "rejected": [],
                 "summary": ""}
    store.save_gate_run(run)
    events.emit("gate_started", gate_run=run_id, batch=ticket_ids)
    gate_branch = f"orchestra/gate-{run_id}"
    path = project.gate_dir / run_id
    log_dir = project.logs_dir / run_id

    def reject(tid: str, reason: str, conflict: bool = False) -> None:
        t = store.get_ticket(tid)
        if t.state != State.MERGING:
            return
        store.add_note(tid, Note(author="merge_gate", kind="gate", text=reason[-4000:]))
        # A merge conflict is routine: the branch simply needs the base merged in. It goes
        # straight back to the ticket's worker and does not count as a review
        # round, so it never triggers the repeated-rejection escalation.
        rounds = t.review_rounds if conflict else t.review_rounds + 1
        store.transition(tid, t.version, State.REJECTED, rejected_by="merge_gate", feedback=reason[-8000:],
                         review_rounds=rounds, gate_rejections=t.gate_rejections + 1)
        run["rejected"].append(tid)
        if conflict:
            run.setdefault("conflicts", []).append(tid)

    try:
        base_commit = git.rev_parse(base)
        run["base_commit"] = base_commit
        git.worktree_add(gate_branch, path, base)
        merged: list[str] = []
        for tid in ticket_ids:
            t = store.get_ticket(tid)
            p = git.merge_no_ff(t.reviewed_commit or t.branch or "", path, f"Merge {tid}: {t.title}")
            if p.returncode != 0:
                git.abort_merge(path)
                reject(tid, f"Your branch conflicts with `{base}` because another ticket landed first. This is "
                            f"routine. In your worktree run `git merge {base}`, resolve every conflict so both "
                            f"sides' changes survive, run the tests, commit the merge, then call report_ticket "
                            f"with READY_FOR_REVIEW.\n\n{p.stdout}\n{p.stderr}", conflict=True)
            else:
                merged.append(tid)
        if not merged:
            run["status"] = "conflict"
            run["summary"] = f"{', '.join(run['rejected'])} conflicted with {base}; sent back to their workers to merge {base}"
            events.emit("gate_conflict", gate_run=run_id, tickets=list(run["rejected"]))
            return run

        ok = _stages(project, path, log_dir, run)
        if ok:
            gate_commit = git.rev_parse("HEAD", cwd=path)
            run["gate_commit"] = gate_commit
            git.land(base, gate_commit, base_commit)
            if cfg.push:
                git.run("push", "origin", base, check=False)
            for tid in merged:
                t = store.get_ticket(tid)
                store.transition(tid, t.version, State.DONE)
                store.add_note(tid, Note(author="merge_gate", kind="gate", text=f"landed on {base} as {gate_commit[:10]}"))
                wt = store.worktree(tid)
                if wt:
                    git.worktree_remove(Path(wt["path"]))
                    store.remove_worktree(tid)
                if t.branch:
                    git.branch_delete(t.branch)
                run["merged"].append(tid)
            run["status"] = "landed"
            run["summary"] = f"landed {', '.join(merged)} on {base} at {gate_commit[:10]}"
            events.emit("gate_landed", gate_run=run_id, tickets=merged, commit=gate_commit)
            if run.get("artifact") and cfg.staging_configured:
                build = {"id": run_id, "gate_run_id": run_id, "commit": gate_commit,
                         "artifact_path": run["artifact"], "created_at": now()}
                record_build(store, build)
                run["staging"] = "deployed" if Staging(project, store, events).deploy(build) else "failed"
            return run

        # Failure: is the base itself broken?
        failed = [s for s in run["stages"] if s["status"] == "failed"]
        failure_text = "\n\n".join(f"[{s['stage']}:{s['name']}] exit {s.get('returncode')}\n{s.get('tail', '')}" for s in failed)
        base_path = project.gate_dir / f"{run_id}-base"
        base_ok = True
        try:
            git.worktree_add(f"orchestra/gate-{run_id}-base", base_path, base)
            base_run = {"id": run_id, "stages": []}
            base_ok = _stages(project, base_path, log_dir / "base", base_run)
            run["base_check"] = base_run["stages"]
        finally:
            git.worktree_remove(base_path)
            git.branch_delete(f"orchestra/gate-{run_id}-base")
        if not base_ok:
            for tid in merged:
                t = store.get_ticket(tid)
                store.transition(tid, t.version, State.APPROVED)
            run["status"] = "base_broken"
            run["summary"] = f"{base} fails the gate with nothing merged; pipeline paused"
            store.set_pipeline(True, f"{base} fails the gate (run {run_id})")
            store.events.attention("supervisor", f"{base} is broken: gate run {run_id} failed with no branches applied")
            events.emit("gate_base_broken", gate_run=run_id)
            return run
        for tid in merged:
            reject(tid, f"Merge gate failed for {tid} on top of {base} (run {run_id}):\n\n{failure_text}")
        run["status"] = "rejected"
        run["summary"] = f"rejected {', '.join(merged)}: {', '.join(s['stage'] + ':' + s['name'] for s in failed)}"
        events.emit("gate_rejected", gate_run=run_id, tickets=merged)
        return run
    except GitError as e:
        run["status"] = "error"
        run["summary"] = str(e)
        for tid in ticket_ids:
            t = store.get_ticket(tid)
            if t.state == State.MERGING:
                store.transition(tid, t.version, State.APPROVED)
        store.set_pipeline(True, f"gate run {run_id} git error: {e}")
        store.events.attention("supervisor", f"gate run {run_id} hit a git error and the pipeline is paused: {e}. "
                         "Fix the cause (or ask the human to), then call resume_pipeline.")
        events.emit("gate_error", gate_run=run_id, error=str(e))
        return run
    finally:
        git.worktree_remove(path)
        git.branch_delete(gate_branch)
        run["finished_at"] = now()
        store.save_gate_run(run)
