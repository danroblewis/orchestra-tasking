#!/usr/bin/env python3
"""A fake `claude -p` for end-to-end tests of the Claude Code harness.

Accepts the flags tasking.harness.claude_code passes, reads the prompt from stdin,
and emits `--output-format stream-json` events. It plays the role named in the MCP
config's env (ORCHESTRA_ROLE) exactly like fake_pi.py plays it, but every Orchestra
tool call goes through the real MCP server named in that config (JSON-RPC over stdio),
so the server is exercised end to end. It appends a Claude-format transcript under
CLAUDE_CONFIG_DIR/projects/<cwd>/<uuid>.jsonl, where the harness expects it.

Env knobs shared with fake_pi: FAKE_PI_WORKER_BLOCK, FAKE_PI_REJECT_FIRST, FAKE_PI_BREAK_FIRST."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid as uuidlib
from datetime import UTC, datetime
from pathlib import Path


def arg(flag: str) -> str | None:
    a = sys.argv[1:]
    return a[a.index(flag) + 1] if flag in a else None


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


class Mcp:
    """Minimal MCP client over stdio, enough to call Orchestra's tools."""

    def __init__(self, server: dict):
        self.p = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, env={**os.environ, **server.get("env", {})}, bufsize=1)
        self._id = 0
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fake-claude", "version": "0"}})
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

    def call(self, name: str, args: dict) -> str:
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        return "".join(c.get("text", "") for c in r.get("result", {}).get("content", []))

    def close(self) -> None:
        self.p.stdin.close()
        self.p.wait(timeout=10)


def main() -> None:
    sid = arg("--session-id") or arg("--resume") or str(uuidlib.uuid4())
    prompt = sys.stdin.read()
    mcp_cfg = json.loads(Path(arg("--mcp-config")).read_text())["mcpServers"]["orchestra"]
    env = mcp_cfg.get("env", {})
    role, ticket = env.get("ORCHESTRA_ROLE", ""), env.get("ORCHESTRA_TICKET", "")
    cwd = Path.cwd()
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    tdir = projects / re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))   # as Claude Code names it
    tdir.mkdir(parents=True, exist_ok=True)
    transcript = tdir / f"{sid}.jsonl"
    if arg("--session-id") and any(projects.glob(f"*/{sid}.jsonl")):     # as Claude Code refuses it
        print(f"Error: Session ID {sid} is already in use.", file=sys.stderr)
        sys.exit(1)

    def record(entry_type: str, content) -> None:
        with transcript.open("a") as f:
            f.write(json.dumps({"type": entry_type, "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                                "message": {"role": entry_type, "content": content}, "sessionId": sid}) + "\n")

    emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": str(cwd)})
    record("user", prompt)
    mcp = Mcp(mcp_cfg)
    tools = {t["name"] for t in mcp.rpc("tools/list", {}).get("result", {}).get("tools", [])}

    def use_tool(name: str, args: dict) -> str:
        assert name in tools, f"{role} was not granted {name}; has {sorted(tools)}"
        tid = f"toolu_{uuidlib.uuid4().hex[:12]}"
        full = f"mcp__orchestra__{name}"
        emit({"type": "stream_event", "event": {"type": "message_start"}, "session_id": sid})
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": ""}}})
        use = [{"type": "tool_use", "id": tid, "name": full, "input": args}]
        emit({"type": "assistant", "message": {"role": "assistant", "content": use}, "session_id": sid})
        record("assistant", use)
        out = mcp.call(name, args)
        res = [{"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": out}]}]
        emit({"type": "user", "message": {"role": "user", "content": res}, "session_id": sid})
        record("user", res)
        return out

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
            if (cwd / ".git").exists():                  # a code ticket's worktree; a task's folder is not a checkout
                subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
                subprocess.run(["git", "commit", "-qm", f"{ticket}: fake work"], cwd=cwd, check=True)
            else:
                (cwd / "RESULT.md").write_text(f"# {ticket}\n\nthe answer\n")
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
    emit({"type": "stream_event", "event": {"type": "message_start"}, "session_id": sid})
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})
    emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}, "session_id": sid})
    record("assistant", [{"type": "text", "text": text}])
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid, "duration_ms": 1})
    time.sleep(0.05)


if __name__ == "__main__":
    main()
