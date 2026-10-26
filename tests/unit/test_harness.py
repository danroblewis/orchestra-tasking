"""The harness layer: normalized events, transcript parsing, the MCP server, and the
tool catalogue, for every harness Orchestra supports."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tasking.config import init_project
from tasking.harness import HARNESSES, SessionSpec, get_harness, read_transcript
from tasking.harness.claude_code import ClaudeCodeHarness, ClaudeCodeSession, _Normalizer as ClaudeNormalizer, transcript_path
from tasking.harness.codex import CodexHarness, CodexSession, _Normalizer as CodexNormalizer, toml_string
from tasking.harness.pi import PiHarness, _Normalizer as PiNormalizer
from tasking.harness.tools import TOOLS, tools_for
from tasking.store import LocalStore


def test_registry():
    assert set(HARNESSES) == {"pi", "claude_code", "codex"}
    assert get_harness(None).name == "pi"
    assert get_harness("claude-code").name == "claude_code"
    assert get_harness("codex").name == "codex"
    with pytest.raises(ValueError):
        get_harness("opencode")


def test_tools_are_grouped_and_schema_shaped():
    names = [t.name for t in TOOLS]
    assert len(names) == len(set(names))
    assert [t.name for t in tools_for(["report_ticket"])] == ["report_ticket"]
    assert {t.group for t in tools_for(["tickets", "escalate"])} == {"tickets", "escalate"}
    d = next(t for t in TOOLS if t.name == "create_ticket").to_dict()
    assert d["inputSchema"]["type"] == "object" and "milestone_id" in d["inputSchema"]["required"]
    assert d["inputSchema"]["additionalProperties"] is False
    # Pi's extension and other harnesses read the catalogue from the CLI.
    out = subprocess.run([sys.executable, "-m", "tasking", "tools", "--json", "--groups", "report_review"],
                         capture_output=True, text=True, check=True).stdout
    assert [t["name"] for t in json.loads(out)] == ["report_review"]


def collect(normalizer_cls, events):
    seen = []
    n = normalizer_cls(lambda e: seen.append(e))
    for e in events:
        n(e)
    return seen


def test_pi_events_normalize():
    seen = collect(PiNormalizer, [
        {"type": "response", "command": "prompt", "success": True},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update"},
        {"type": "tool_execution_start", "toolName": "bash", "args": {"command": "ls"}},
        {"type": "tool_execution_end", "toolName": "bash"},
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}, {"type": "toolCall", "name": "report_ticket"}]}},
        {"type": "message_end", "message": {"role": "user", "content": "ignored"}},
    ])
    assert [e["type"] for e in seen] == ["turn_start", "assistant_start", "token", "tool_start", "tool_end", "assistant_end"]
    assert seen[3] == {"type": "tool_start", "name": "bash", "args": {"command": "ls"}}
    assert seen[-1] == {"type": "assistant_end", "text": "hi", "tool_calls": ["report_ticket"]}


def test_claude_events_normalize():
    seen = collect(ClaudeNormalizer, [
        {"type": "system", "subtype": "init", "session_id": "u"},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {}}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__orchestra__project_status", "input": {}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}},
        {"type": "rate_limit_event"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
        {"type": "result", "subtype": "success", "result": "done"},
    ])
    assert [e["type"] for e in seen] == ["turn_start", "assistant_start", "token", "tool_start", "assistant_end", "tool_end", "assistant_end"]
    assert seen[3]["name"] == "project_status"            # the MCP prefix is dropped
    assert seen[5] == {"type": "tool_end", "name": "project_status"}


def test_codex_events_normalize():
    seen = collect(CodexNormalizer, [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "Model metadata not found"}},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "Looking.\n"}},
        {"type": "item.started", "item": {"id": "item_2", "type": "mcp_tool_call", "server": "orchestra", "tool": "project_status", "arguments": {}, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "mcp_tool_call", "server": "orchestra", "tool": "project_status", "arguments": {}, "status": "completed"}},
        {"type": "item.started", "item": {"id": "item_3", "type": "command_execution", "command": "/bin/zsh -lc ls", "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_3", "type": "command_execution", "command": "/bin/zsh -lc ls", "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_4", "type": "reasoning", "text": "..."}},
        {"type": "item.completed", "item": {"id": "item_5", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    ])
    kinds = [e["type"] for e in seen]
    assert kinds == ["token", "turn_start", "token", "assistant_end", "token", "tool_start", "token", "tool_end",
                     "token", "tool_start", "token", "tool_end", "token", "token", "assistant_end"]
    assert seen[5] == {"type": "tool_start", "name": "project_status", "args": {}}
    assert seen[9] == {"type": "tool_start", "name": "exec", "args": {"command": "/bin/zsh -lc ls"}}
    assert seen[-1] == {"type": "assistant_end", "text": "done", "tool_calls": []}


def test_pi_streams_reply_text_through_token_events():
    from tasking.harness.pi import _Normalizer
    got = []
    n = _Normalizer(got.append)
    n({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "hmm"}})
    n({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Creating "}})
    n({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "the ticket."}})
    assert [(e["type"], e.get("kind"), e.get("delta")) for e in got] == [
        ("token", "thinking", "hmm"), ("token", "text", "Creating "), ("token", "text", "the ticket.")]


def test_pi_transcript(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in [
        {"type": "session", "id": "x"},
        {"type": "message", "timestamp": 1.0, "message": {"role": "user", "content": "do it"}},
        {"type": "message", "timestamp": 2.0, "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}, {"type": "toolCall", "name": "bash", "arguments": "{\"command\": \"ls\"}"}]}},
        {"type": "message", "timestamp": 3.0, "message": {"role": "toolResult", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "a.py"}]}},
    ]))
    msgs = PiHarness.read_transcript(f)
    assert [m["role"] for m in msgs] == ["user", "assistant", "toolResult"]
    assert msgs[1]["tool_calls"] == [{"name": "bash", "args": {"command": "ls"}}]
    assert msgs[2]["tool_name"] == "bash" and msgs[2]["text"] == "a.py"
    assert read_transcript({"harness": "pi", "session_file": str(f)}) == msgs
    assert read_transcript({"session_file": str(f)}) == msgs     # pi is the default


def test_claude_transcript(tmp_path):
    f = tmp_path / "u.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in [
        {"type": "queue-operation"},
        {"type": "user", "timestamp": "2026-10-11T10:51:53.231Z", "message": {"role": "user", "content": "do it"}},
        {"type": "attachment"},
        {"type": "assistant", "timestamp": "2026-10-11T10:51:54.000Z", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "mcp__orchestra__create_milestone", "input": {"name": "M"}}]}},
        {"type": "user", "timestamp": "2026-10-11T10:51:55.000Z", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "{\"ok\": true}"}]}]}},
        {"type": "assistant", "timestamp": "2026-10-11T10:51:56.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Created M1."}]}},
    ]))
    msgs = ClaudeCodeHarness.read_transcript(f)
    assert [m["role"] for m in msgs] == ["user", "assistant", "toolResult", "assistant"]
    assert msgs[1]["tool_calls"] == [{"name": "create_milestone", "args": {"name": "M"}}]
    assert msgs[2]["tool_name"] == "create_milestone" and msgs[2]["text"] == '{"ok": true}'
    assert msgs[0]["ts"].startswith("2026-10-11")
    assert read_transcript({"harness": "claude_code", "session_file": str(f)}) == msgs


def test_codex_transcript(tmp_path):
    """Shapes captured from Codex 0.162 rollouts: context messages are skipped, MCP calls
    come from McpToolCall items once, Codex's own tools from function_call records."""
    f = tmp_path / "rollout-2026-10-11T12-23-21-t.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in [
        {"timestamp": "2026-10-11T19:23:21.871Z", "type": "session_meta", "payload": {"id": "t", "cwd": "/w"}},
        {"timestamp": "2026-10-11T19:23:22.700Z", "type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<skills_instructions>..."}]}},
        {"timestamp": "2026-10-11T19:23:22.702Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>\n  <cwd>/w</cwd>\n</environment_context>"}]}},
        {"timestamp": "2026-10-11T19:23:22.813Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "do it"}]}},
        {"timestamp": "2026-10-11T19:23:27.795Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "I'll run ls.\n"}]}},
        {"timestamp": "2026-10-11T19:23:27.810Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": "{\"cmd\":\"ls\"}", "call_id": "call_1"}},
        {"timestamp": "2026-10-11T19:23:27.894Z", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "CommandExecution", "id": "call_1", "command": ["/bin/zsh", "-lc", "ls"]}}},
        {"timestamp": "2026-10-11T19:23:27.929Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_1", "output": "README.md\n"}},
        {"timestamp": "2026-10-11T19:23:28.000Z", "type": "response_item", "payload": {"type": "function_call", "name": "mcp__orchestra__create_milestone", "arguments": "{\"name\": \"M\"}", "call_id": "call_2"}},
        {"timestamp": "2026-10-11T19:23:28.100Z", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "McpToolCall", "id": "call_2", "server": "orchestra", "tool": "create_milestone", "arguments": {"name": "M"}, "status": "completed", "result": {"content": [{"type": "text", "text": "{\"ok\": true}"}], "isError": False}}}},
        {"timestamp": "2026-10-11T19:23:28.200Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_2", "output": "{\"ok\": true}"}},
        {"timestamp": "2026-10-11T19:23:28.305Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Created M1."}]}},
        {"timestamp": "2026-10-11T19:23:28.486Z", "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Created M1."}},
    ]))
    msgs = CodexHarness.read_transcript(f)
    assert [m["role"] for m in msgs] == ["user", "assistant", "toolResult", "assistant", "toolResult", "assistant"]
    assert msgs[0]["text"] == "do it" and msgs[0]["ts"].startswith("2026-10-11")
    assert msgs[1]["text"] == "I'll run ls.\n" and msgs[1]["tool_calls"] == [{"name": "exec_command", "args": {"cmd": "ls"}}]
    assert msgs[2]["tool_name"] == "exec_command" and msgs[2]["text"] == "README.md\n"
    assert msgs[3]["tool_calls"] == [{"name": "create_milestone", "args": {"name": "M"}}]   # once, prefix dropped
    assert msgs[4]["tool_name"] == "create_milestone" and msgs[4]["text"] == '{"ok": true}' and not msgs[4]["is_error"]
    assert msgs[5]["text"] == "Created M1."
    assert read_transcript({"harness": "codex", "session_file": str(f)}) == msgs


def test_codex_session_identity_resume_and_args(tmp_path, monkeypatch):
    import tomllib
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "wt").mkdir()
    s = CodexSession(spec(tmp_path))
    s.open()
    assert s.thread_id is None and s.session_file is None       # Codex assigns the thread id on the first turn
    args = s._args()
    assert args[:3] == [s.binary, "exec", "--json"] and args[-1] == "-" and "resume" not in args
    assert args[args.index("-C") + 1] == str(tmp_path / "wt") and args[args.index("-m") + 1] == "haiku"
    assert args[args.index("--sandbox") + 1] == "danger-full-access"
    overrides = dict(a.partition("=")[::2] for a in args[args.index("-c") + 1::2] if "=" in a)
    mcp = tomllib.loads("v = " + overrides["mcp_servers.orchestra"])["v"]
    assert mcp["command"] == sys.executable and mcp["args"] == ["-m", "tasking.harness.mcp"] and mcp["required"] is True
    assert mcp["env"]["ORCHESTRA_TOOLS"] == "report_ticket" and mcp["env"]["ORCHESTRA_TICKET"] == "T-1"
    assert tomllib.loads("v = " + overrides["developer_instructions"])["v"] == "be good"
    # The first turn's thread.started is remembered; the rollout is found under CODEX_HOME.
    rollout = tmp_path / "codex" / "sessions" / "2026" / "10" / "11" / "rollout-2026-10-11T12-00-00-abc.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    s._remember("abc")
    assert s.thread_id == "abc" and s.session_file == str(rollout)
    again = CodexSession(spec(tmp_path))
    again.open()
    assert again.thread_id == "abc" and again.session_file == str(rollout)
    assert again._args()[-3:] == ["resume", "abc", "-"]
    assert again.pid is None and again.alive()
    again.close()
    assert not again.alive()


def test_codex_prompt_text_survives_toml():
    import tomllib
    text = 'Line one\n\t"quoted" and back\\slash, unicode: é 🙂 and a # hash [table]'
    assert tomllib.loads("v = " + toml_string(text))["v"] == text


def test_codex_turn_without_binary_reports_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "wt").mkdir()
    s = CodexSession(spec(tmp_path), binary=str(tmp_path / "no-such-codex"))
    res = s.prompt("hi", timeout=5)
    assert res.error and "cannot start" in res.error and not res.settled


def spec(tmp_path, **kw) -> SessionSpec:
    return SessionSpec(session_id=kw.get("session_id", "worker-T-1"), session_dir=tmp_path / "sessions", cwd=tmp_path / "wt",
                       system_prompt="be good", role="worker", tool_groups=("report_ticket",),
                       env={"ORCHESTRA_ROLE": "worker", "ORCHESTRA_REPO": str(tmp_path), "ORCHESTRA_TICKET": "T-1"},
                       log_path=tmp_path / "logs" / "x.log", model="haiku", compact_tokens=kw.get("compact_tokens", 50_000))


def test_claude_session_identity_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "wt").mkdir()
    s = ClaudeCodeSession(spec(tmp_path))
    s.open()
    assert s.uuid and s.session_file == str(transcript_path(tmp_path / "wt", s.uuid))
    args = s._args()
    assert args[:2] == [s.binary, "-p"] and "--session-id" in args and "--resume" not in args
    assert "--model" in args and args[args.index("--model") + 1] == "haiku"
    assert "--strict-mcp-config" in args and "--dangerously-skip-permissions" in args
    assert args[args.index("--autocompact") + 1] == "100000" and s.compact() == {"mode": "auto", "limit": 100000}   # the CLI's floor
    cfg = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())["mcpServers"]["orchestra"]
    assert cfg["command"] == sys.executable and cfg["env"]["ORCHESTRA_TOOLS"] == "report_ticket"
    # Same session id later: same uuid; once Claude has written the transcript, we resume it.
    again = ClaudeCodeSession(spec(tmp_path))
    again.open()
    assert again.uuid == s.uuid
    Path(again.session_file).parent.mkdir(parents=True)
    Path(again.session_file).write_text("{}\n")
    assert "--resume" in again._args() and "--session-id" not in again._args()
    assert again.pid is None and again.alive()
    again.close()
    assert not again.alive()


def test_claude_turn_without_binary_reports_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "wt").mkdir()
    s = ClaudeCodeSession(spec(tmp_path), binary=str(tmp_path / "no-such-claude"))
    res = s.prompt("hi", timeout=5)
    assert res.error and "cannot start" in res.error and not res.settled


class McpClient:
    def __init__(self, env):
        self.p = subprocess.Popen([sys.executable, "-m", "tasking.harness.mcp"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, env=env, bufsize=1)
        self.n = 0

    def rpc(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return init_project(repo, provider="p", model="m", prefix="T")


def test_mcp_server_serves_the_role_tools(project, tmp_path):
    import os
    base = {**os.environ, "ORCHESTRATOR_HOME": str(tmp_path / "home"), "ORCHESTRA_REPO": str(project.repo_root)}
    c = McpClient({**base, "ORCHESTRA_ROLE": "supervisor", "ORCHESTRA_TOOLS": "tickets,context"})
    try:
        init = c.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        assert init["result"]["serverInfo"]["name"] == "orchestra" and "tools" in init["result"]["capabilities"]
        names = {t["name"] for t in c.rpc("tools/list")["result"]["tools"]}
        assert "create_milestone" in names and "get_project_context" in names and "report_ticket" not in names
        r = c.rpc("tools/call", {"name": "create_milestone", "arguments": {"name": "Via MCP"}})["result"]
        assert not r.get("isError") and json.loads(r["content"][0]["text"])["milestone"]["id"] == "M1"
        r = c.rpc("tools/call", {"name": "report_ticket", "arguments": {}})["result"]   # not granted
        assert r["isError"]
        r = c.rpc("tools/call", {"name": "create_ticket", "arguments": {"title": "x", "milestone_id": "M9"}})["result"]
        assert r["isError"] and "M9" in r["content"][0]["text"]                           # store error surfaces as text
        assert "error" in c.rpc("nonsense")
    finally:
        c.close()
    assert LocalStore(project).get_milestone("M1").name == "Via MCP"
    # The worker's exit tool goes through the same server with the ticket from the env.
    store = LocalStore(project)
    from tasking.model import State
    t = store.create_ticket(title="w", milestone_id="M1")
    store.transition(t.id, t.version, State.IN_PROGRESS)   # reports are only accepted from a live worker
    w = McpClient({**base, "ORCHESTRA_ROLE": "worker", "ORCHESTRA_TOOLS": "report_ticket", "ORCHESTRA_TICKET": t.id})
    try:
        w.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        assert [x["name"] for x in w.rpc("tools/list")["result"]["tools"]] == ["report_ticket"]
        r = w.rpc("tools/call", {"name": "report_ticket", "arguments": {"state": "READY_FOR_REVIEW", "summary": "done"}})["result"]
        assert not r.get("isError"), r
    finally:
        w.close()
    assert store.pop_report(t.id)["state"] == "READY_FOR_REVIEW"
