"""Staging: one long-lived instance of the most recent build that passed the gate.
SPEC.md section 7.5. Supervised by the loop; redeployed by the gate on every landing."""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import psutil

from .config import Project
from .events import Events
from .model import now
from .store import LocalStore

HEALTH_STALE = 600        # seconds a running staging process may go without a healthy answer


def _epoch(iso: str) -> float:
    try:
        return _dt.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _probe(url: str, timeout: float = 6, max_status: int = 400) -> bool:
    """HTTP probe: alive when the status is below ``max_status`` (default: 2xx/3xx only,
    so a 404 on the health path is unhealthy). Uses urllib first; on a 403 from a
    Cloudflare edge, or an SSL problem from a bare Python, it confirms through curl,
    which uses the system trust store and a browser-like identity."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < max_status
    except urllib.error.HTTPError as e:
        if e.code != 403:
            return e.code < max_status
    except Exception:
        pass
    if url.startswith("https") and shutil.which("curl"):
        p = subprocess.run(["curl", "-s", "-o", "/dev/null", "-m", str(int(timeout)), "-w", "%{http_code}", url],
                           capture_output=True, text=True)
        code = int(p.stdout.strip()) if p.stdout.strip().isdigit() else 0
        return 100 <= code < max_status   # curl prints 000 when it could not connect at all
    return False


def build_python(project: Project, artifact: str | None) -> str:
    """Interpreter to run an artifact with. For a wheel or sdist, a persistent venv is
    created next to the artifact (artifacts/<build>/venv) with the artifact installed,
    so long-lived processes never depend on a uv temp environment that may vanish.
    For anything else, the current interpreter."""
    import sys
    if not artifact or not artifact.endswith((".whl", ".tar.gz")) or not shutil.which("uv"):
        return sys.executable
    venv = Path(artifact).parent / "venv"
    py = venv / "bin" / "python"
    if not py.exists():
        subprocess.run(["uv", "venv", "-q", str(venv)], check=True, capture_output=True, text=True)
        subprocess.run(["uv", "pip", "install", "-q", "--python", str(py), artifact], check=True,
                       capture_output=True, text=True)
    return str(py)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Staging:
    def __init__(self, project: Project, store: LocalStore, events: Events):
        self.project = project
        self.store = store
        self.events = events
        self.log_path = project.staging_dir / "staging.log"
        self._last_probe = 0.0

    @property
    def cfg(self):
        # Always the project's current config: the loop reloads orchestra.toml every
        # tick, and a stale snapshot here once restarted staging with an old command.
        return self.project.config

    # -- state -----------------------------------------------------------------

    def state(self) -> dict:
        s = self.store.runtime().get("staging") or {}
        s.setdefault("status", "stopped")
        s["alive"] = _pid_alive(s.get("pid"))
        s["configured"] = self.cfg.staging_configured
        return s

    def _set(self, **fields) -> dict:
        def fn(d):
            st = d.setdefault("staging", {})
            st.update(fields)
            return dict(st)
        return self.store._mutate_runtime(fn)

    def _log(self, line: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{now()}] [tasking] {line}\n")

    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.staging_port}"

    def public_url(self) -> str | None:
        return f"https://{self.cfg.tunnel_hostname}" if self.cfg.tunnel_hostname else None

    # -- process control -------------------------------------------------------

    def _group_pids(self, pgid: int) -> list[int]:
        out = []
        for proc in psutil.process_iter(["pid"]):
            try:
                if os.getpgid(proc.info["pid"]) == pgid:
                    out.append(proc.info["pid"])
            except (ProcessLookupError, PermissionError, psutil.Error):
                continue
        return out

    def _port_listeners(self, port: int) -> list[int]:
        p = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True)
        return [int(x) for x in p.stdout.split() if x.isdigit()]

    def stop(self) -> None:
        """Stop the staging process group completely, then anything still holding the
        staging port. A loop started inside the group that survives SIGTERM, or an
        instance whose record was lost, must not linger and mask the next deploy."""
        st = self.state()
        pid = st.get("pid")
        if pid:
            self._log(f"stopping build {st.get('current_build_id')} (pid {pid})")
            if self.cfg.staging_stop:
                subprocess.run(self.cfg.staging_stop.format(port=self.cfg.staging_port, pid=pid), shell=True,
                               cwd=str(self.project.repo_root), capture_output=True, timeout=60)
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
            for _ in range(20):
                if not self._group_pids(pid):
                    break
                time.sleep(0.5)
            for p in self._group_pids(pid):
                try:
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    pass
        for p in self._port_listeners(self.cfg.staging_port):
            self._log(f"killing stray listener on port {self.cfg.staging_port} (pid {p})")
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        self._set(pid=None, status="stopped")

    def _start(self, build: dict) -> bool:
        port = self.cfg.staging_port
        try:
            python = build_python(self.project, build.get("artifact_path"))
        except subprocess.CalledProcessError as e:
            self._log(f"could not provision a venv for build {build['id']}: {e.stderr[-800:]}")
            self._set(status="failed", pid=None)
            return False
        cmd = self.cfg.staging_start.format(artifact=build.get("artifact_path", ""), port=port, python=python)
        self._log(f"starting build {build['id']} ({build.get('commit', '')[:10]}): {cmd}")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        out = self.log_path.open("a", encoding="utf-8")
        proc = subprocess.Popen(cmd, shell=True, cwd=str(self.project.repo_root), stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
        self._set(pid=proc.pid, port=port, status="starting", current_build_id=build["id"],
                  local_url=self.local_url(), public_url=self.public_url(), deployed_at=now())
        url = self.local_url() + self.cfg.staging_health
        deadline = time.time() + self.cfg.staging_health_timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                self._log(f"process exited with {proc.returncode} before becoming healthy")
                self._set(status="failed", pid=None)
                return False
            if _probe(url):
                self._log(f"healthy at {url}")
                self._set(status="running", last_health_at=now())
                return True
            time.sleep(1)
        self._log(f"health probe {url} did not pass within {self.cfg.staging_health_timeout}s")
        self._set(status="failed")
        return False

    # -- tunnel ----------------------------------------------------------------

    def _tunnel_id(self, name: str) -> str | None:
        p = subprocess.run(["cloudflared", "tunnel", "list", "-o", "json"], capture_output=True, text=True, timeout=30)
        try:
            for t in json.loads(p.stdout or "[]"):
                if t.get("name") == name:
                    return t.get("id")
        except json.JSONDecodeError:
            pass
        return None

    def ensure_tunnel(self) -> bool:
        """Run the named cloudflared tunnel against the staging port and probe it.

        The tunnel, its credentials file, and the DNS route are operator setup
        (`cloudflared tunnel create` / `tunnel route dns`); Orchestra only writes
        the ingress config for this port and keeps the process running."""
        name, host = self.cfg.tunnel_name, self.cfg.tunnel_hostname
        if not (name and host):
            return True
        public = f"https://{host}{self.cfg.staging_health}"
        st = self.state()
        if _pid_alive(st.get("tunnel_pid")) and _probe(public, max_status=500):
            self._set(public_url=f"https://{host}")
            return True
        if not shutil.which("cloudflared"):
            self._log("cloudflared not on PATH; skipping tunnel")
            return False
        tid = self._tunnel_id(name)
        if not tid:
            self._log(f"no cloudflared tunnel named {name!r}; create it with `cloudflared tunnel create {name}` "
                      f"and route it with `cloudflared tunnel route dns {name} {host}`")
            return False
        cred_file = Path("~/.cloudflared").expanduser() / f"{tid}.json"
        if not cred_file.exists():
            self._log(f"credentials file {cred_file} missing for tunnel {name}")
            return False
        cfg = self.project.staging_dir / "tunnel.yml"
        cfg.write_text(f"tunnel: {tid}\ncredentials-file: {cred_file}\ningress:\n"
                       f"  - hostname: {host}\n    service: {self.local_url()}\n  - service: http_status:404\n")
        if not _pid_alive(st.get("tunnel_pid")):
            self._log(f"starting cloudflared tunnel {name} ({tid[:8]}) -> https://{host}")
            out = (self.project.staging_dir / "tunnel.log").open("a", encoding="utf-8")
            proc = subprocess.Popen(["cloudflared", "--no-autoupdate", "--config", str(cfg), "tunnel", "run", name],
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            self._set(tunnel_pid=proc.pid)
        deadline = time.time() + 180  # a freshly routed hostname can take a while to resolve
        while time.time() < deadline:
            if _probe(public, max_status=500):
                self._log(f"tunnel healthy at {public}")
                self._set(public_url=f"https://{host}")
                return True
            time.sleep(3)
        self._log(f"tunnel probe {public} failed; see {self.project.staging_dir / 'tunnel.log'}")
        return False

    @contextmanager
    def _exclusive(self):
        """One deploy/restart at a time per project, across processes (the gate in the
        loop and a manual `tasking staging deploy` must not interleave)."""
        self.project.staging_dir.mkdir(parents=True, exist_ok=True)
        with (self.project.staging_dir / ".deploy.lock").open("a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # -- public ----------------------------------------------------------------

    def deploy(self, build: dict) -> bool:
        with self._exclusive():
            return self._deploy(build)

    def _deploy(self, build: dict) -> bool:
        """Promote a build: stop the previous, start this one, probe health and tunnel.
        On failure restart the previous build and wake the Pipeline Manager."""
        if not self.cfg.staging_configured:
            return False
        prev_id = self.state().get("current_build_id")
        self.stop()
        ok = self._start(build) and self.ensure_tunnel()
        if ok:
            self._set(previous_build_id=prev_id)
            self.events.emit("staging_deployed", build=build["id"], commit=build.get("commit"),
                             local_url=self.local_url(), public_url=self.public_url())
            return True
        self.events.emit("staging_failed", build=build["id"])
        self.store.add_wakeup("pipeline_manager",
                              f"staging deploy of build {build['id']} failed (see staging log); rolled back")
        prev = self.store.runtime().get("builds", {}).get(prev_id or "")
        if prev:
            self.stop()
            self._log(f"rolling back to build {prev_id}")
            self._start(prev)
        return False

    def ensure_running(self) -> None:
        """Called every tick: restart a staging process that died. Takes the deploy lock
        without waiting, so it never races a deploy or restart already in progress."""
        if not self.cfg.staging_configured:
            return
        st = self.state()
        if st.get("status") in ("running", "starting") and not st["alive"]:
            self.project.staging_dir.mkdir(parents=True, exist_ok=True)
            with (self.project.staging_dir / ".deploy.lock").open("a+") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return  # a deploy/restart owns staging right now
                try:
                    st = self.state()
                    if st["alive"]:
                        return
                    build = self.store.runtime().get("builds", {}).get(st.get("current_build_id") or "")
                    if build:
                        self._log("staging process died; restarting")
                        self.stop()
                        self._start(build)
                        self.ensure_tunnel()
                        self.events.emit("staging_restarted", build=build["id"])
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        elif st.get("status") == "running" and st["alive"]:
            # Probe health every 15s, not every tick, so the staging log is not
            # dominated by Orchestra's own health requests.
            if time.time() - self._last_probe >= 15:
                self._last_probe = time.time()
                if _probe(self.local_url() + self.cfg.staging_health):
                    self._set(last_health_at=now())
                else:
                    # alive by pid but not answering: after HEALTH_STALE without a healthy
                    # answer the build has failed in service; say so once and let the
                    # restart path take it from here
                    last = st.get("last_health_at")
                    stale = last is None or time.time() - _epoch(last) > HEALTH_STALE
                    if stale and st.get("current_build_id") and st.get("failed_build") != st.get("current_build_id"):
                        self._log(f"staging stopped answering its health check for {HEALTH_STALE // 60} minutes; recording the failure and restarting")
                        self._set(failed_build=st.get("current_build_id"))
                        self.events.emit("staging_failed", build=st["current_build_id"], reason=f"health check unanswered for {HEALTH_STALE // 60} minutes")
                        self.stop()
            if self.cfg.tunnel_name and not _pid_alive(st.get("tunnel_pid")):
                self.ensure_tunnel()

    def restart(self) -> str:
        """Returns 'ok', 'no_build', 'start_failed', or 'tunnel_failed'."""
        with self._exclusive():
            return self._restart()

    def _restart(self) -> str:
        build = self.store.runtime().get("builds", {}).get(self.state().get("current_build_id") or "")
        if not build:
            return "no_build"
        self.stop()
        if not self._start(build):
            return "start_failed"
        return "ok" if self.ensure_tunnel() else "tunnel_failed"

    def logs(self, n: int = 20000) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-n:] if self.log_path.exists() else ""


def record_build(store: LocalStore, build: dict) -> None:
    store._mutate_runtime(lambda d: d.setdefault("builds", {}).__setitem__(build["id"], build))
