"""The Pi harness: one ``pi --mode rpc`` process per live session.

Pi stays resident between turns (the process is the session), takes one JSON command
per line on stdin and streams events on stdout; ``agent_settled`` ends a turn. Tools
come from the Orchestra extension (``pi_extension.ts``), which registers the tool
definitions in ``tools.py`` and runs each call through ``tasking api``."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .base import (
    AgentSession,
    Harness,
    OnEvent,
    SessionSpec,
    TurnResult,
    _Deadlines,
    assistant_text,
)

EXTENSION_PATH = Path(__file__).parent / "pi_extension.ts"


class PiError(RuntimeError):
    pass


class PiSession(AgentSession):
    def __init__(self, spec: SessionSpec, pi_binary: str | None = None):
        self.spec = spec
        args = [pi_binary or os.environ.get("PI_BINARY", "pi"), "--mode", "rpc",
                "--session-dir", str(spec.session_dir), "--session-id", spec.session_id,
                "--extension", str(EXTENSION_PATH)]
        if spec.provider:
            args += ["--provider", spec.provider]
        if spec.model:
            args += ["--model", spec.model]
        if spec.thinking:
            args += ["--thinking", spec.thinking]
        if spec.system_prompt:
            args += ["--append-system-prompt", spec.system_prompt]
        full_env = {**os.environ, **spec.env, "ORCHESTRA_TOOLS": ",".join(spec.tool_groups),
                    "ORCHESTRA_PYTHON": sys.executable}
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr = spec.log_path.open("a", encoding="utf-8")
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr, text=True, cwd=str(spec.cwd), env=full_env, bufsize=1)
        self._q: queue.Queue[dict | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self._lock = threading.Lock()
        self.session_file = None
        self.busy = False

    @property
    def harness_name(self) -> str:
        return "pi"

    # -- plumbing --------------------------------------------------------------

    def _read(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                self._q.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self._q.put(None)

    def _send(self, cmd: dict) -> None:
        if self.proc.poll() is not None or self.proc.stdin is None:
            raise PiError(f"pi process for {self.session_id} is not running")
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()

    def _next(self, timeout: float) -> dict | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return {}

    def _reply_ui(self, ev: dict) -> None:
        rid = ev.get("id")   # never block on an extension dialog: cancel it
        if rid is not None:
            try:
                self._send({"type": "extension_ui_response", "id": rid, "cancelled": True})
            except PiError:
                pass

    def command(self, cmd: dict, timeout: float = 30) -> dict:
        with self._lock:
            self._send(cmd)
            deadline = time.time() + timeout
            while time.time() < deadline:
                ev = self._next(1.0)
                if ev is None:
                    raise PiError("pi exited")
                if ev.get("type") == "response" and ev.get("command") == cmd["type"]:
                    return ev
                if ev.get("type") == "extension_ui_request":
                    self._reply_ui(ev)
            raise PiError(f"timeout waiting for {cmd['type']}")

    # -- interface -------------------------------------------------------------

    def open(self) -> None:
        r = self.command({"type": "get_state"})
        data = r.get("data", {}) if r.get("success") else {}
        self.session_file = data.get("sessionFile") or self.session_file

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc.poll() is None else None

    def alive(self) -> bool:
        return self.proc.poll() is None

    def prompt(self, text: str, timeout: float | None, on_event: OnEvent | None = None,
               idle_timeout: float | None = None) -> TurnResult:
        with self._lock:
            self.busy = True
            result = TurnResult()
            emit = _Normalizer(on_event)
            try:
                self._send({"type": "prompt", "message": text})
                clock = _Deadlines(timeout, idle_timeout)
                last_text = ""
                while True:
                    why = clock.expired()
                    if why:
                        result.timed_out, result.stopped_by, result.text = True, why, last_text
                        self._abort()
                        return result
                    ev = self._next(clock.wait())
                    if ev is None:
                        result.error = "pi exited"
                        return result
                    if not ev:
                        continue
                    clock.touch()
                    t = ev.get("type")
                    emit(ev)
                    if t == "response" and ev.get("command") == "prompt" and not ev.get("success", True):
                        result.error = f"prompt rejected: {ev.get('error')}"
                        return result
                    if t == "extension_ui_request":
                        self._reply_ui(ev)
                    elif t == "message_end":
                        msg = ev.get("message", {})
                        if msg.get("role") == "assistant":
                            txt = assistant_text(msg.get("content"))
                            if txt.strip():
                                last_text = txt
                            for p in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                                if isinstance(p, dict) and p.get("type") == "toolCall":
                                    result.tool_calls.append(p.get("name", "?"))
                    elif t == "agent_settled":
                        result.settled = True
                        result.text = last_text
                        emit.settled(last_text)
                        return result
            finally:
                self.busy = False

    def compact(self) -> dict | None:
        """Pi's own compaction: older messages become a summary entry in the session file and
        the context is rebuilt from it, so the agent keeps what it learned. Waits for the
        response; summarising is a model call and can take a while on a loaded server."""
        with self._lock:
            try:
                self._send({"type": "compact"})
            except PiError:
                return None
            clock = _Deadlines(1800, 900)
            while not clock.expired():
                ev = self._next(clock.wait())
                if ev is None:
                    return None
                if not ev:
                    continue
                clock.touch()
                if ev.get("type") == "extension_ui_request":
                    self._reply_ui(ev)
                elif ev.get("type") == "response" and ev.get("command") == "compact":
                    if not ev.get("success", True):
                        return None
                    d = ev.get("data") or {}
                    return {"tokens_before": d.get("tokensBefore"), "tokens_after": d.get("estimatedTokensAfter")}
            return None

    def _abort(self) -> None:
        try:
            self._send({"type": "abort"})
            deadline = time.time() + 15
            while time.time() < deadline:
                ev = self._next(1.0)
                if ev is None or (ev and ev.get("type") == "agent_settled"):
                    return
                if ev and ev.get("type") == "response" and ev.get("command") == "abort":
                    return
        except PiError:
            pass

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            self._stderr.close()
        except OSError:
            pass


class _Normalizer:
    """Pi RPC events -> normalized events."""

    def __init__(self, on_event: OnEvent | None):
        self.on_event = on_event

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
        msg = ev.get("message") or {}
        if t == "response" and ev.get("command") == "prompt" and ev.get("success", True):
            self._emit({"type": "turn_start"})
        elif t == "message_start" and msg.get("role") == "assistant":
            self._emit({"type": "assistant_start"})
        elif t == "message_update":
            u = ev.get("assistantMessageEvent") or {}
            kind = {"text_delta": "text", "thinking_delta": "thinking"}.get(u.get("type"))
            self._emit({"type": "token", "kind": kind, "delta": u.get("delta") if kind else None})
        elif t == "tool_execution_start":
            self._emit({"type": "tool_start", "name": ev.get("toolName") or ev.get("name", "?"),
                        "args": ev.get("args") or ev.get("arguments") or {}})
        elif t == "tool_execution_end":
            self._emit({"type": "tool_end", "name": ev.get("toolName") or ev.get("name", "?")})
        elif t == "message_end" and msg.get("role") == "assistant":
            content = msg.get("content")
            calls = [p.get("name", "?") for p in content if isinstance(p, dict) and p.get("type") == "toolCall"] \
                if isinstance(content, list) else []
            self._emit({"type": "assistant_end", "text": assistant_text(content), "tool_calls": calls})


class PiHarness(Harness):
    name = "pi"

    def __init__(self, binary: str | None = None):
        self.binary = binary

    def start(self, spec: SessionSpec) -> PiSession:
        return PiSession(spec, self.binary)

    @staticmethod
    def read_transcript(session_file: str | Path) -> list[dict]:
        """Parse a Pi session JSONL into [{role, text, tool_calls, ts}]."""
        out: list[dict] = []
        p = Path(session_file)
        if not p.exists():
            return out
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") != "message":
                continue
            msg = e.get("message", {})
            role = msg.get("role")
            if role not in ("user", "assistant", "toolResult"):
                continue
            content = msg.get("content")
            text, tools = "", []
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text += part.get("text", "")
                    elif part.get("type") == "toolCall":
                        args = part.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                pass
                        tools.append({"name": part.get("name"), "args": args})
            entry = {"role": role, "text": text, "tool_calls": tools, "ts": e.get("timestamp")}
            if role == "toolResult":
                entry["text"] = text[:6000]
                entry["tool_name"] = msg.get("toolName")
                entry["is_error"] = bool(msg.get("isError"))
            out.append(entry)
        return out
