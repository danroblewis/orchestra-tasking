#!/usr/bin/env python3
"""A fake `pi --mode rpc` for end-to-end tests.

Speaks just enough of the RPC protocol for tasking.harness.pi: `get_state`, `prompt`,
`abort`. On `prompt` it plays the role given by ORCHESTRA_ROLE:

- worker: edits a file in the cwd (its worktree), commits, then calls
  `tasking api report_ticket` exactly like the real extension tool would.
  If FAKE_PI_WORKER_BLOCK is set it reports BLOCKED instead. If FAKE_PI_BREAK_FIRST
  is set, its first attempt also deletes README.md (which the e2e unit command
  requires), so the merge gate rejects it; the next attempt restores the file.
- reviewer: calls `tasking api report_review` with APPROVED, or REJECTED the first
  time if FAKE_PI_REJECT_FIRST is set (tracked by a marker file in the worktree).
- supervisor / coordinator: replies with text only.

It writes a Pi-format session file so transcripts work."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def api(cmd: str, args: dict) -> dict:
    py = os.environ.get("ORCHESTRA_PYTHON", sys.executable)
    p = subprocess.run([py, "-m", "tasking", "api", cmd, "--compact", "--args", json.dumps(args)],
                       capture_output=True, text=True, cwd=os.environ.get("ORCHESTRA_REPO"))
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "error": p.stdout + p.stderr}


def main() -> None:
    argv = sys.argv[1:]
    session_dir = Path(argv[argv.index("--session-dir") + 1])
    session_id = argv[argv.index("--session-id") + 1]
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.jsonl"
    role = os.environ.get("ORCHESTRA_ROLE", "")
    ticket = os.environ.get("ORCHESTRA_TICKET", "")

    def record(role_: str, text: str, tool: str | None = None) -> None:
        content = [{"type": "text", "text": text}]
        if tool:
            content.append({"type": "toolCall", "name": tool, "arguments": {}})
        with session_file.open("a") as f:
            f.write(json.dumps({"type": "message", "timestamp": time.time(),
                                "message": {"role": role_, "content": content}}) + "\n")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        cmd = json.loads(line)
        t = cmd.get("type")
        if t == "get_state":
            emit({"type": "response", "command": "get_state", "success": True,
                  "data": {"sessionFile": str(session_file), "sessionId": session_id}})
        elif t == "abort":
            emit({"type": "response", "command": "abort", "success": True})
        elif t == "prompt":
            emit({"type": "response", "command": "prompt", "success": True})
            record("user", cmd.get("message", ""))
            text, tool = "ok", None
            if role == "worker":
                cwd = Path.cwd()
                if os.environ.get("FAKE_PI_WORKER_BLOCK"):
                    api("report_ticket", {"state": "BLOCKED", "summary": "cannot", "blocked_reason": "need a decision"})
                    text, tool = "I am blocked.", "report_ticket"
                else:
                    f = cwd / "WORK.md"
                    f.write_text((f.read_text() if f.exists() else "") + f"work on {ticket}\n")
                    broke = cwd / ".fake_broke"
                    if os.environ.get("FAKE_PI_BREAK_FIRST") and not broke.exists():
                        broke.write_text("1")
                        (cwd / "README.md").unlink()
                    elif broke.exists() and not (cwd / "README.md").exists():
                        (cwd / "README.md").write_text("# e2e\n")
                    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
                    subprocess.run(["git", "commit", "-qm", f"{ticket}: fake work"], cwd=cwd, check=True)
                    api("report_ticket", {"state": "READY_FOR_REVIEW", "summary": "added WORK.md line"})
                    text, tool = "Done, reported ready.", "report_ticket"
            elif role == "reviewer":
                marker = Path.cwd() / ".fake_reviewed"
                if os.environ.get("FAKE_PI_REJECT_FIRST") and not marker.exists():
                    marker.write_text("1")
                    api("report_review", {"verdict": "REJECTED", "summary": "needs work", "feedback": "1. add more"})
                    text, tool = "Rejected.", "report_review"
                else:
                    api("report_review", {"verdict": "APPROVED", "summary": "looks good"})
                    text, tool = "Approved.", "report_review"
            else:
                text = f"{role} acknowledges: {cmd.get('message', '')[:60]}"
            record("assistant", text, tool)
            emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
            emit({"type": "agent_settled"})
        else:
            emit({"type": "response", "command": t, "success": True})


if __name__ == "__main__":
    main()
