"""The Claude Code harness: ``claude -p`` headless, one process per turn, resumed by id.

Claude Code has no resident process: each turn is ``claude -p`` reading the prompt from
stdin, streaming ``--output-format stream-json`` events, and exiting. The session is the
transcript Claude keeps under its own config directory, resumed with ``--resume``.
Orchestra's tools reach the model through the MCP server in ``mcp.py``."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .base import AgentSession, Harness, OnEvent, SessionSpec, TurnResult, _Deadlines

TOOL_PREFIX = "mcp__orchestra__"


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def transcript_path(cwd: Path, session_uuid: str) -> Path:
    """Where Claude Code writes the session: projects/<cwd with / as ->/<uuid>.jsonl."""
    key = str(Path(cwd).resolve()).replace("/", "-")
    return claude_config_dir() / "projects" / key / f"{session_uuid}.jsonl"


class ClaudeCodeSession(AgentSession):
    def __init__(self, spec: SessionSpec, binary: str | None = None):
        self.spec = spec
        self.binary = binary or os.environ.get("CLAUDE_BINARY", "claude")
        self._dir = spec.session_dir / "claude-code"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._meta_path = self._dir / f"{spec.session_id}.json"
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self._closed = False
        self.busy = False
        self.session_file = None
        self.uuid: str | None = None

    @property
    def harness_name(self) -> str:
        return "claude_code"

    # -- session identity --------------------------------------------------------

    def open(self) -> None:
        """Create or load the session's uuid. Claude writes the transcript on the first turn."""
        if self._meta_path.exists():
            meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            self.uuid = meta["uuid"]
        else:
            self.uuid = str(uuid.uuid4())
            self._meta_path.write_text(json.dumps({"uuid": self.uuid, "cwd": str(self.spec.cwd),
                                                   "session_id": self.spec.session_id}) + "\n", encoding="utf-8")
        self.session_file = str(transcript_path(self.spec.cwd, self.uuid))

    @property
    def pid(self) -> int | None:
        p = self.proc
        return p.pid if p is not None and p.poll() is None else None

    def alive(self) -> bool:
        return not self._closed

    # -- one turn ------------------------------------------------------------------

    def _mcp_config(self) -> Path:
        env = {k: v for k, v in self.spec.env.items()}
        env["ORCHESTRA_TOOLS"] = ",".join(self.spec.tool_groups)
        for k in ("ORCHESTRATOR_HOME", "PATH", "HOME"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        cfg = {"mcpServers": {"orchestra": {"command": sys.executable, "args": ["-m", "tasking.harness.mcp"], "env": env}}}
        p = self._dir / f"{self.spec.session_id}.mcp.json"
        p.write_text(json.dumps(cfg, indent=1) + "\n", encoding="utf-8")
        return p

    def compact(self) -> dict | None:
        """Claude Code compacts its own context past the --autocompact window it was started
        with; there is no command to ask for it between turns. Reporting that keeps the
        loop from rotating a session the harness is already keeping bounded."""
        if not self.spec.compact_tokens:
            return None
        return {"mode": "auto", "limit": max(100_000, int(self.spec.compact_tokens))}

    def _args(self) -> list[str]:
        if self.uuid is None:
            self.open()
        resume = self.session_file and Path(self.session_file).exists()
        args = [self.binary, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--dangerously-skip-permissions", "--strict-mcp-config", "--mcp-config", str(self._mcp_config())]
        if self.spec.model:
            args += ["--model", self.spec.model]
        if self.spec.compact_tokens:
            args += ["--autocompact", str(max(100_000, int(self.spec.compact_tokens)))]
        if self.spec.system_prompt:
            sp = self._dir / f"{self.spec.session_id}.system.md"
            sp.write_text(self.spec.system_prompt, encoding="utf-8")
            args += ["--append-system-prompt-file", str(sp)]
        args += ["--resume", self.uuid] if resume else ["--session-id", self.uuid]
        return args

    def prompt(self, text: str, timeout: float | None, on_event: OnEvent | None = None,
               idle_timeout: float | None = None) -> TurnResult:
        with self._lock:
            self.busy = True
            result = TurnResult()
            emit = _Normalizer(on_event)
            self.spec.log_path.parent.mkdir(parents=True, exist_ok=True)
            stderr = self.spec.log_path.open("a", encoding="utf-8")
            try:
                self.proc = subprocess.Popen(self._args(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                             text=True, cwd=str(self.spec.cwd), env={**os.environ, **self.spec.env},
                                             bufsize=1, start_new_session=True)
            except OSError as e:
                result.error = f"cannot start {self.binary}: {e}"
                stderr.close()
                return result
            try:
                try:
                    self.proc.stdin.write(text)  # type: ignore[union-attr]
                    self.proc.stdin.close()      # type: ignore[union-attr]
                except (BrokenPipeError, OSError):
                    pass
                clock = _Deadlines(timeout, idle_timeout)
                reader = _LineReader(self.proc.stdout)
                last_text = ""
                while True:
                    why = clock.expired()
                    if why:
                        result.timed_out, result.stopped_by, result.text = True, why, last_text
                        self.kill()
                        return result
                    line = reader.next(clock.wait())
                    if line is None:          # EOF: the process exited
                        break
                    if not line:
                        continue
                    clock.touch()
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    emit(ev)
                    t = ev.get("type")
                    if t == "assistant":
                        for part in (ev.get("message") or {}).get("content") or []:
                            if part.get("type") == "tool_use":
                                result.tool_calls.append(_short(part.get("name", "?")))
                            elif part.get("type") == "text" and part.get("text", "").strip():
                                last_text = part["text"]
                    elif t == "result":
                        if ev.get("is_error"):
                            result.error = str(ev.get("result") or ev.get("subtype") or "error")
                        else:
                            last_text = ev.get("result") or last_text
                rc = self.proc.wait()
                if rc != 0 and not result.error:
                    result.error = f"claude exited with status {rc}"
                if result.error:
                    result.text = last_text
                    return result
                result.settled = True
                result.text = last_text
                emit.settled(last_text)
                return result
            finally:
                self.proc = None
                stderr.close()
                self.busy = False

    def kill(self) -> None:
        p = self.proc
        if p is None or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=5)
        except Exception:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                pass

    def close(self) -> None:
        self.kill()
        self._closed = True


class _LineReader:
    def __init__(self, stream):
        import queue
        self._q: queue.Queue[str | None] = queue.Queue()
        self._queue_mod = queue

        def run():
            for line in stream:
                self._q.put(line.rstrip("\n"))
            self._q.put(None)
        threading.Thread(target=run, daemon=True).start()

    def next(self, timeout: float) -> str | None:
        try:
            return self._q.get(timeout=timeout)
        except self._queue_mod.Empty:
            return ""


def _short(name: str) -> str:
    return name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name


class _Normalizer:
    """Claude Code stream-json events -> normalized events."""

    def __init__(self, on_event: OnEvent | None):
        self.on_event = on_event
        self._pending: dict[str, str] = {}   # tool_use id -> name

    def _emit(self, ev: dict) -> None:
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def settled(self, text: str) -> None:
        self._emit({"type": "settled", "text": text})

    def __call__(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            self._emit({"type": "turn_start"})
        elif t == "stream_event":
            inner = (ev.get("event") or {}).get("type")
            if inner == "message_start":
                self._emit({"type": "assistant_start"})
            elif inner == "content_block_delta":
                self._emit({"type": "token"})
        elif t == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            calls = []
            for part in content:
                if part.get("type") == "tool_use":
                    name = _short(part.get("name", "?"))
                    calls.append(name)
                    self._pending[part.get("id", "")] = name
                    self._emit({"type": "tool_start", "name": name, "args": part.get("input") or {}})
            text = "".join(p.get("text", "") for p in content if p.get("type") == "text")
            if text.strip() or calls:
                self._emit({"type": "assistant_end", "text": text, "tool_calls": calls})
        elif t == "user":
            content = (ev.get("message") or {}).get("content")
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    name = self._pending.pop(part.get("tool_use_id", ""), "?")
                    self._emit({"type": "tool_end", "name": name})


class ClaudeCodeHarness(Harness):
    name = "claude_code"

    def __init__(self, binary: str | None = None):
        self.binary = binary

    def start(self, spec: SessionSpec) -> ClaudeCodeSession:
        return ClaudeCodeSession(spec, self.binary)

    @staticmethod
    def read_transcript(session_file: str | Path) -> list[dict]:
        """Parse a Claude Code session JSONL into [{role, text, tool_calls, ts}]."""
        out: list[dict] = []
        p = Path(session_file)
        if not p.exists():
            return out
        names: dict[str, str] = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = e.get("type")
            if t not in ("user", "assistant"):
                continue
            msg = e.get("message") or {}
            content = msg.get("content")
            ts = e.get("timestamp")
            if t == "assistant":
                text, tools = "", []
                for part in content if isinstance(content, list) else []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text += part.get("text", "")
                    elif part.get("type") == "tool_use":
                        name = _short(part.get("name", "?"))
                        names[part.get("id", "")] = name
                        tools.append({"name": name, "args": part.get("input")})
                if text or tools:
                    out.append({"role": "assistant", "text": text, "tool_calls": tools, "ts": ts})
                continue
            if isinstance(content, str):
                if content.strip():
                    out.append({"role": "user", "text": content, "tool_calls": [], "ts": ts})
                continue
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_result":
                    body = part.get("content")
                    if isinstance(body, list):
                        body = "".join(b.get("text", "") for b in body if isinstance(b, dict) and b.get("type") == "text")
                    out.append({"role": "toolResult", "text": str(body or "")[:6000], "tool_calls": [], "ts": ts,
                                "tool_name": names.get(part.get("tool_use_id", ""), "?"),
                                "is_error": bool(part.get("is_error"))})
                elif part.get("type") == "text" and part.get("text", "").strip():
                    out.append({"role": "user", "text": part["text"], "tool_calls": [], "ts": ts})
        return out
