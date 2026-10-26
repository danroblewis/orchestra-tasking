"""The Codex harness: ``codex exec --json``, one process per turn, resumed by thread id.

Codex has no resident process either: each turn is ``codex exec`` reading the prompt from
stdin, streaming ``--json`` events (``thread.started``, ``turn.started``, ``item.*``,
``turn.completed`` / ``turn.failed``), and exiting. Codex assigns the thread id on the first
turn; later turns are ``codex exec resume <thread_id>``. Orchestra's tools reach the model
through the MCP server in ``mcp.py``, declared with a ``-c mcp_servers.orchestra=...``
override, and the role prompt rides along as ``developer_instructions``. The transcript is
Codex's own rollout file under ``$CODEX_HOME/sessions/``."""

from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .base import AgentSession, Harness, OnEvent, SessionSpec, TurnResult, _Deadlines

MCP_SERVER = "orchestra"
TOOL_PREFIX = f"mcp__{MCP_SERVER}__"      # how Codex names an MCP server's functions
_INJECTED = re.compile(r"\s*<[a-z_]+>")    # <environment_context>, <user_instructions>: Codex's own context messages
_TOOL_ITEMS = {"command_execution": "exec", "mcp_tool_call": None, "file_change": "apply_patch", "web_search": "web_search"}


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def rollout_path(thread_id: str) -> Path | None:
    """Codex writes ``sessions/YYYY/MM/DD/rollout-<timestamp>-<thread_id>.jsonl``."""
    hits = glob.glob(str(codex_home() / "sessions" / "*" / "*" / "*" / f"rollout-*-{thread_id}.jsonl"))
    return Path(max(hits)) if hits else None


def toml_string(text: str) -> str:
    """A TOML basic string: JSON's escaping is a subset of TOML's once non-ASCII stays literal."""
    return json.dumps(text, ensure_ascii=False)


def _short(name: str) -> str:
    return name.removeprefix(TOOL_PREFIX)


def _item_tool(item: dict) -> tuple[str, dict] | None:
    """(tool name, args) for an item that represents a tool call, else None."""
    kind = item.get("type")
    if kind not in _TOOL_ITEMS:
        return None
    if kind == "mcp_tool_call":
        return _short(item.get("tool") or "?"), item.get("arguments") or {}
    if kind == "command_execution":
        return "exec", {"command": item.get("command")}
    if kind == "file_change":
        return "apply_patch", {"changes": item.get("changes")}
    return "web_search", {"query": item.get("query")}


class CodexSession(AgentSession):
    def __init__(self, spec: SessionSpec, binary: str | None = None):
        self.spec = spec
        self.binary = binary or os.environ.get("CODEX_BINARY", "codex")
        self._dir = spec.session_dir / "codex"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._meta_path = self._dir / f"{spec.session_id}.json"
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self._closed = False
        self.busy = False
        self.session_file = None
        self.thread_id: str | None = None

    @property
    def harness_name(self) -> str:
        return "codex"

    # -- session identity --------------------------------------------------------

    def open(self) -> None:
        """Load the thread id if a first turn already ran. Codex assigns it on that turn."""
        if self._meta_path.exists():
            meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            self.thread_id = meta.get("thread_id")
            self.session_file = meta.get("session_file")
        if self.thread_id and not (self.session_file and Path(self.session_file).exists()):
            p = rollout_path(self.thread_id)
            if p:
                self.session_file = str(p)

    def _remember(self, thread_id: str) -> None:
        self.thread_id = thread_id
        p = rollout_path(thread_id)
        self.session_file = str(p) if p else self.session_file
        self._meta_path.write_text(json.dumps({"thread_id": thread_id, "cwd": str(self.spec.cwd),
                                               "session_id": self.spec.session_id, "session_file": self.session_file}) + "\n",
                                   encoding="utf-8")

    @property
    def pid(self) -> int | None:
        p = self.proc
        return p.pid if p is not None and p.poll() is None else None

    def alive(self) -> bool:
        return not self._closed

    # -- one turn ------------------------------------------------------------------

    def _mcp_override(self) -> str:
        env = {k: v for k, v in self.spec.env.items()}
        env["ORCHESTRA_TOOLS"] = ",".join(self.spec.tool_groups)
        for k in ("ORCHESTRATOR_HOME", "PATH", "HOME"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        env_toml = ", ".join(f"{k}={toml_string(v)}" for k, v in env.items())
        args = ", ".join(toml_string(a) for a in ["-m", "tasking.harness.mcp"])
        return (f"mcp_servers.{MCP_SERVER}={{command={toml_string(sys.executable)}, args=[{args}], "
                f"env={{{env_toml}}}, required=true}}")

    def _args(self) -> list[str]:
        if self.thread_id is None and self._meta_path.exists():
            self.open()
        args = [self.binary, "exec", "--json", "--skip-git-repo-check", "--sandbox", "danger-full-access",
                "-C", str(self.spec.cwd), "-c", self._mcp_override()]
        if self.spec.system_prompt:
            args += ["-c", f"developer_instructions={toml_string(self.spec.system_prompt)}"]
        if self.spec.model:
            args += ["-m", self.spec.model]
        if self.spec.provider:
            args += ["-c", f"model_provider={toml_string(self.spec.provider)}"]
        if self.spec.thinking:
            args += ["-c", f"model_reasoning_effort={toml_string(self.spec.thinking)}"]
        args += ["resume", self.thread_id, "-"] if self.thread_id else ["-"]
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
                    item = ev.get("item") or {}
                    if t == "thread.started" and ev.get("thread_id") and ev["thread_id"] != self.thread_id:
                        self._remember(ev["thread_id"])
                    elif t == "item.completed" and item.get("type") == "agent_message" and (item.get("text") or "").strip():
                        last_text = item["text"]
                    elif t == "item.started" and (tool := _item_tool(item)):
                        result.tool_calls.append(tool[0])
                    elif t == "turn.failed":
                        result.error = str((ev.get("error") or {}).get("message") or "turn failed")
                    elif t == "error":
                        result.error = str(ev.get("message") or "error")
                rc = self.proc.wait()
                if self.thread_id and not (self.session_file and Path(self.session_file).exists()):
                    self._remember(self.thread_id)   # the rollout exists once the first turn has run
                if rc != 0 and not result.error:
                    result.error = f"codex exited with status {rc}"
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


class _Normalizer:
    """Codex ``--json`` events -> normalized events. Codex streams whole items, not tokens,
    so every item event counts as liveness (``token``)."""

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
        item = ev.get("item") or {}
        if t == "turn.started":
            self._emit({"type": "turn_start"})
        elif t == "item.started":
            self._emit({"type": "token"})
            if tool := _item_tool(item):
                self._emit({"type": "tool_start", "name": tool[0], "args": tool[1]})
        elif t == "item.completed":
            self._emit({"type": "token"})
            if item.get("type") == "agent_message":
                text = item.get("text") or ""
                if text.strip():
                    self._emit({"type": "assistant_end", "text": text, "tool_calls": []})
            elif tool := _item_tool(item):
                self._emit({"type": "tool_end", "name": tool[0]})


class CodexHarness(Harness):
    name = "codex"

    def __init__(self, binary: str | None = None):
        self.binary = binary

    def start(self, spec: SessionSpec) -> CodexSession:
        return CodexSession(spec, self.binary)

    @staticmethod
    def read_transcript(session_file: str | Path) -> list[dict]:
        """Parse a Codex rollout into [{role, text, tool_calls, ts}].

        ``response_item`` lines carry the model-facing items: user and assistant messages,
        ``function_call`` / ``function_call_output`` for Codex's own tools. MCP calls are
        taken from the ``McpToolCall`` items (server, tool, arguments, result) and the
        matching function-call records are skipped so each call appears once."""
        out: list[dict] = []
        p = Path(session_file)
        if not p.exists():
            return out
        names: dict[str, str] = {}
        mcp_ids: set[str] = set()
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = e.get("timestamp")
            pl = e.get("payload") or {}
            kind = pl.get("type")
            if e.get("type") == "event_msg" and kind == "item_completed" and (pl.get("item") or {}).get("type") == "McpToolCall":
                it = pl["item"]
                cid = it.get("id", "")
                mcp_ids.add(cid)
                name = _short(it.get("tool") or "?")
                _tool_call(out, name, it.get("arguments") or {}, ts)
                res = it.get("result") or {}
                body = "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
                if it.get("error"):
                    body = body or str(it["error"])
                out.append({"role": "toolResult", "text": body[:6000], "tool_calls": [], "ts": ts, "tool_name": name,
                            "is_error": bool(it.get("error") or res.get("isError"))})
                continue
            if e.get("type") != "response_item":
                continue
            if kind == "message":
                role = pl.get("role")
                text = "".join(c.get("text", "") for c in pl.get("content") or [] if isinstance(c, dict))
                if role == "assistant" and text.strip():
                    out.append({"role": "assistant", "text": text, "tool_calls": [], "ts": ts})
                elif role == "user" and text.strip() and not _INJECTED.match(text):
                    out.append({"role": "user", "text": text, "tool_calls": [], "ts": ts})
            elif kind in ("function_call", "custom_tool_call"):
                cid = pl.get("call_id", "")
                name = pl.get("name") or "?"
                if cid in mcp_ids or name.startswith("mcp__"):
                    mcp_ids.add(cid)
                    continue
                raw = pl.get("arguments") if kind == "function_call" else pl.get("input")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except json.JSONDecodeError:
                    args = {"input": raw}
                names[cid] = name
                _tool_call(out, name, args, ts)
            elif kind in ("function_call_output", "custom_tool_call_output"):
                cid = pl.get("call_id", "")
                if cid in mcp_ids:
                    continue
                body = pl.get("output")
                if isinstance(body, list):
                    body = "".join(b.get("text", "") for b in body if isinstance(b, dict))
                out.append({"role": "toolResult", "text": str(body or "")[:6000], "tool_calls": [], "ts": ts,
                            "tool_name": names.get(cid, "?"), "is_error": False})
        return out


def _tool_call(out: list[dict], name: str, args, ts) -> None:
    """Attach a tool call to the assistant message that issued it, or start one."""
    call = {"name": name, "args": args}
    if out and out[-1]["role"] == "assistant":
        out[-1]["tool_calls"].append(call)
    else:
        out.append({"role": "assistant", "text": "", "tool_calls": [call], "ts": ts})
