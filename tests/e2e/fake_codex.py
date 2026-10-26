#!/usr/bin/env python3
"""A fake `codex exec` for end-to-end tests of the Codex harness.

Accepts the arguments tasking.harness.codex passes (`exec --json ... -c key=value ...
[resume <thread_id>] -`), reads the prompt from stdin, and emits `--json` events the way
Codex 0.162 does. It plays the role named in the MCP override's env (ORCHESTRA_ROLE)
exactly like fake_pi.py and fake_claude.py play it, calling every Orchestra tool through
the real MCP server named in that override. It appends a Codex-format rollout under
CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl, where the harness expects it.

Env knobs shared with fake_pi: FAKE_PI_WORKER_BLOCK, FAKE_PI_REJECT_FIRST, FAKE_PI_BREAK_FIRST."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tomllib
import uuid as uuidlib
from datetime import UTC, datetime
from pathlib import Path


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def overrides() -> dict:
    """The -c key=value overrides, parsed as TOML values."""
    out = {}
    a = sys.argv[1:]
    for i, x in enumerate(a):
        if x == "-c" and i + 1 < len(a):
            k, _, v = a[i + 1].partition("=")
            out[k] = tomllib.loads(f"v = {v}")["v"]
    return out


class Mcp:
    """Minimal MCP client over stdio, enough to call Orchestra's tools."""

    def __init__(self, server: dict):
        self.p = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, env={**os.environ, **server.get("env", {})}, bufsize=1)
        self._id = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake-codex", "version": "0"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()

    def rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("mcp server exited")
            msg = json.loads(line)
            if msg.get("id") == self._id:
                return msg

    def call(self, name: str, args: dict) -> dict:
        return self.rpc("tools/call", {"name": name, "arguments": args}).get("result", {})

    def close(self) -> None:
        self.p.stdin.close()
        self.p.wait(timeout=10)


def main() -> None:
    a = sys.argv[1:]
    assert a[:2] == ["exec", "--json"], a
    thread_id = a[a.index("resume") + 1] if "resume" in a else str(uuidlib.uuid4())
    prompt = sys.stdin.read()
    ov = overrides()
    server = ov["mcp_servers.orchestra"]
    env = server.get("env", {})
    role, ticket = env.get("ORCHESTRA_ROLE", ""), env.get("ORCHESTRA_TICKET", "")
    cwd = Path(a[a.index("-C") + 1]) if "-C" in a else Path.cwd()
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    now = datetime.now(UTC)
    sdir = home / "sessions" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    sdir.mkdir(parents=True, exist_ok=True)
    existing = sorted(sdir.glob(f"rollout-*-{thread_id}.jsonl"))
    rollout = existing[-1] if existing else sdir / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{thread_id}.jsonl"
    turn_id = str(uuidlib.uuid4())
    ordinal = [0]

    def record(entry_type: str, payload: dict) -> None:
        with rollout.open("a") as f:
            f.write(json.dumps({"timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                                "ordinal": ordinal[0], "type": entry_type, "payload": payload}) + "\n")
        ordinal[0] += 1

    if not existing:
        record("session_meta", {"session_id": thread_id, "id": thread_id, "cwd": str(cwd), "originator": "codex_exec"})
    # Codex's own context messages precede the prompt; the transcript parser must skip them.
    record("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"<environment_context>\n  <cwd>{cwd}</cwd>\n</environment_context>"}]})
    if "developer_instructions" in ov and not existing:
        record("response_item", {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": ov["developer_instructions"]}]})
    record("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]})
    emit({"type": "thread.started", "thread_id": thread_id})
    emit({"type": "turn.started"})
    mcp = Mcp(server)
    tools = {t["name"] for t in mcp.rpc("tools/list", {}).get("result", {}).get("tools", [])}
    n = [0]

    def say(text: str) -> None:
        n[0] += 1
        emit({"type": "item.completed", "item": {"id": f"item_{n[0]}", "type": "agent_message", "text": text}})
        record("response_item", {"type": "message", "id": f"msg_{n[0]}", "role": "assistant", "content": [{"type": "output_text", "text": text}]})

    def use_tool(name: str, args: dict) -> str:
        assert name in tools, f"{role} was not granted {name}; has {sorted(tools)}"
        n[0] += 1
        cid = f"call_{uuidlib.uuid4().hex[:12]}"
        base = {"id": f"item_{n[0]}", "type": "mcp_tool_call", "server": "orchestra", "tool": name, "arguments": args, "result": None, "error": None}
        emit({"type": "item.started", "item": {**base, "status": "in_progress"}})
        # The rollout records the model's call and Codex's McpToolCall item for the same call id.
        record("response_item", {"type": "function_call", "id": f"fc_{cid[5:]}", "name": f"mcp__orchestra__{name}", "arguments": json.dumps(args), "call_id": cid})
        res = mcp.call(name, args)
        text = "".join(c.get("text", "") for c in res.get("content", []))
        record("event_msg", {"type": "item_completed", "thread_id": thread_id, "turn_id": turn_id,
                             "item": {"type": "McpToolCall", "id": cid, "server": "orchestra", "tool": name, "arguments": args,
                                      "status": "completed", "result": {"content": res.get("content", []), "isError": bool(res.get("isError"))}}})
        record("response_item", {"type": "function_call_output", "call_id": cid, "output": text})
        emit({"type": "item.completed", "item": {**base, "result": {"content": res.get("content", []), "structured_content": None}, "status": "completed"}})
        return text

    text = "ok"
    if role == "worker":
        if os.environ.get("FAKE_PI_WORKER_BLOCK"):
            use_tool("report_ticket", {"state": "BLOCKED", "summary": "cannot", "blocked_reason": "need a decision"})
            text = "I am blocked."
        else:
            f = cwd / "WORK.md"
            f.write_text((f.read_text() if f.exists() else "") + f"work on {ticket}\n")
            broke = cwd / ".fake_broke"
            if os.environ.get("FAKE_PI_BREAK_FIRST") and not broke.exists():
                broke.write_text("1")
                (cwd / "README.md").unlink()
            elif broke.exists() and not (cwd / "README.md").exists():
                (cwd / "README.md").write_text("# e2e\n")
            n[0] += 1
            emit({"type": "item.started", "item": {"id": f"item_{n[0]}", "type": "command_execution", "command": "/bin/zsh -lc 'git add -A && git commit'", "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
            subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
            subprocess.run(["git", "commit", "-qm", f"{ticket}: fake work"], cwd=cwd, check=True)
            emit({"type": "item.completed", "item": {"id": f"item_{n[0]}", "type": "command_execution", "command": "/bin/zsh -lc 'git add -A && git commit'", "aggregated_output": "", "exit_code": 0, "status": "completed"}})
            use_tool("report_ticket", {"state": "READY_FOR_REVIEW", "summary": "added WORK.md line"})
            text = "Done, reported ready."
    elif role == "reviewer":
        marker = cwd / ".fake_reviewed"
        if os.environ.get("FAKE_PI_REJECT_FIRST") and not marker.exists():
            marker.write_text("1")
            use_tool("report_review", {"verdict": "REJECTED", "summary": "needs work", "feedback": "1. add more"})
            text = "Rejected."
        else:
            use_tool("report_review", {"verdict": "APPROVED", "summary": "looks good"})
            text = "Approved."
    else:
        text = f"{role} acknowledges: {prompt[:60]}"
    mcp.close()
    say(text)
    record("event_msg", {"type": "task_complete", "turn_id": turn_id, "last_agent_message": text})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}})
    time.sleep(0.05)


if __name__ == "__main__":
    main()
