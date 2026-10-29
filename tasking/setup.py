"""``[tasks] worktree_setup``: a shell command run once in each worktree the task system makes or
adopts, right after it exists and before its first use: the pool's slots (made under
``<state>/pool/`` or adopted from ``worktree_paths``), a ticket's own worktree when the pool is off,
and the gate's worktrees. For what the project checkout has and a fresh worktree lacks: local,
uncommitted files (a policy file under ``.claude/``, a ``.env``), built dependencies.

It runs with ``sh -c`` in the worktree, in Orchestra's child environment
(``orchestra.launcher.child_env``) plus ``ORCHESTRA_PROJECT`` (the project root) and
``ORCHESTRA_WORKTREE`` (the worktree), for at most ``worktree_setup_timeout``. A success is
recorded in the worktree's own git directory (``orchestra-setup``: a hash of the command), so it
runs again only when the command changes or the worktree is made again; a pool reset does not run
it (ignored and skip-worktree files survive a reset). A failure leaves the worktree unprepared and
unused: the output's tail goes to the scheduler log and the supervisor is told, once per failure,
and it is tried again after a back-off (a minute, doubling, at most half an hour), never every
tick. SPEC.md section 5."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from orchestra.launcher import child_env

from .adopt import git_dir
from .config import Project
from .git import GitError

RECORD = "orchestra-setup"
BACKOFF_S, BACKOFF_MAX_S = 60.0, 1800.0
TAIL = 2000


class SetupError(GitError):
    """The worktree is not prepared: its setup failed, or waits to be tried again."""


def digest(command: str) -> str:
    return hashlib.sha256(command.encode()).hexdigest()[:16]


def done(path: Path, command: str) -> bool:
    """Whether ``command`` already succeeded in ``path`` (its record in the worktree's git directory)."""
    d = git_dir(path)
    try:
        return d is not None and (d / RECORD).read_text().strip() == digest(command)
    except OSError:
        return False


class Setup:
    def __init__(self, project: Project, log: Callable[[str], None] = print,
                 attention: Callable[[str], None] | None = None):
        self.project, self.log, self.attention = project, log, attention
        self._failed: dict[str, tuple[float, float]] = {}      # path -> (try again at, back-off so far)
        self._running: set[subprocess.Popen] = set()           # setups under way (the scheduler runs them in threads)
        self._lock = threading.Lock()
        self.stopped = False                                    # set by ``stop``: what it ended is no failure

    def waiting(self, path: Path) -> bool:
        """Whether ``path`` failed its setup and its back-off has not run out."""
        f = self._failed.get(str(path))
        return f is not None and time.monotonic() < f[0]

    def ensure(self, path: Path) -> None:
        """``path`` prepared: nothing to do without a command or when it already ran; else run it.
        Raises ``SetupError`` when it fails, or failed and waits for its back-off."""
        command = self.project.config.worktree_setup
        if not command or done(path, command):
            return
        if self.waiting(path):
            raise SetupError(f"worktree_setup failed in {path}; it is tried again later")
        env = child_env({"ORCHESTRA_PROJECT": str(self.project.repo_root), "ORCHESTRA_WORKTREE": str(path)})
        timeout = self.project.config.worktree_setup_timeout
        self.log(f"worktree setup: running in {path}")
        try:
            ok, what, out = self._run(command, path, env, timeout)
        except OSError as e:
            ok, what, out = False, str(e), ""
        if self.stopped and not ok:
            raise SetupError(f"worktree_setup in {path} was stopped: the task system shut down")
        if ok:
            self._failed.pop(str(path), None)
            d = git_dir(path)
            if d is not None:
                (d / RECORD).write_text(digest(command) + "\n")
            return
        backoff = min(BACKOFF_MAX_S, (self._failed.get(str(path), (0.0, BACKOFF_S / 2))[1]) * 2)
        self._failed[str(path)] = (time.monotonic() + backoff, backoff)
        reason = (f"worktree_setup failed in {path} ({what}); the worktree is not used until it succeeds, "
                  f"tried again in {backoff:g}s")
        self.log(f"worktree setup: {reason}\n{out.strip()[-TAIL:]}")
        if self.attention:
            self.attention(f"{reason}: {out.strip()[-500:]}")
        raise SetupError(reason)

    def _run(self, command: str, path: Path, env: dict, timeout: float) -> tuple[bool, str, str]:
        """The command in its own process group, so a timeout or ``stop`` ends all it started."""
        p = subprocess.Popen(["sh", "-c", command], cwd=path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, start_new_session=True)
        with self._lock:
            self._running.add(p)
        try:
            out, _ = p.communicate(timeout=timeout)
            return p.returncode == 0, f"exit {p.returncode}", out or ""
        except subprocess.TimeoutExpired:
            _kill(p)
            out, _ = p.communicate()
            return False, f"timed out after {timeout:g}s", out or ""
        finally:
            with self._lock:
                self._running.discard(p)

    def stop(self) -> None:
        """Every setup under way ended (the scheduler shuts down): a worktree whose setup did not
        finish is not recorded as prepared, so it runs again on its next use."""
        self.stopped = True
        with self._lock:
            running = list(self._running)
        for p in running:
            _kill(p)


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
