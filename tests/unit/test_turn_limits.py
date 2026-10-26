"""A turn ends when the agent settles, when nothing has come from the harness for the
silence limit, or, only if the project set one, at the hard cap. A busy agent is never
cut off without a cap."""

import os
import stat
import textwrap
import time
from pathlib import Path

import pytest

from tasking.harness.base import SessionSpec, _Deadlines
from tasking.harness.pi import PiSession

FAKE = textwrap.dedent('''\
    #!/usr/bin/env python3
    """A Pi that answers the RPC handshake, then behaves per FAKE_MODE: silent (never emits
    after accepting the prompt) or chatty (emits a token every 0.2s forever)."""
    import json, os, sys, time, threading
    mode = os.environ.get("FAKE_MODE", "silent")
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd["type"] == "get_state":
            emit({"type": "response", "command": "get_state", "success": True, "data": {"sessionFile": "/tmp/x.jsonl"}})
        elif cmd["type"] == "prompt":
            emit({"type": "response", "command": "prompt", "success": True})
            if mode == "chatty":
                def chat():
                    while True:
                        emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "."}}); time.sleep(0.2)
                threading.Thread(target=chat, daemon=True).start()
        elif cmd["type"] == "compact":
            emit({"type": "compaction_start", "reason": "manual"})
            emit({"type": "response", "command": "compact", "success": True, "data": {"tokensBefore": 150000, "estimatedTokensAfter": 32000}})
        elif cmd["type"] == "abort":
            emit({"type": "response", "command": "abort", "success": True}); emit({"type": "agent_settled"})
''')


@pytest.fixture
def fake_pi(tmp_path):
    p = tmp_path / "fake_pi.py"
    p.write_text(FAKE)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def spec(tmp_path, sid):
    return SessionSpec(session_id=sid, session_dir=tmp_path / "s", cwd=tmp_path, system_prompt="", role="x", tool_groups=(),
                       env={"ORCHESTRA_REPO": str(tmp_path), "ORCHESTRA_ROLE": "x", "ORCHESTRA_TICKET": ""},
                       log_path=tmp_path / "log" / f"{sid}.log")


def test_deadlines():
    d = _Deadlines(None, None)
    assert d.expired() is None and d.wait() == 5.0                    # nothing can end it but the agent
    d = _Deadlines(0, 0.05)
    time.sleep(0.06)
    assert d.expired() == "silence"
    d.touch()
    assert d.expired() is None                                        # an event resets the silence clock
    d = _Deadlines(0.01, None)
    time.sleep(0.02)
    assert d.expired() == "limit"


def test_silent_agent_is_stopped_after_the_silence_limit(fake_pi, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "silent")
    s = PiSession(spec(tmp_path, "quiet"), pi_binary=str(fake_pi))
    s.open()
    t0 = time.time()
    res = s.prompt("go", None, idle_timeout=1.0)
    assert res.timed_out and res.stopped_by == "silence" and time.time() - t0 < 5
    s.close()


def test_pi_compacts_on_request(fake_pi, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "silent")
    s = PiSession(spec(tmp_path, "big"), pi_binary=str(fake_pi))
    s.open()
    assert s.compact() == {"tokens_before": 150000, "tokens_after": 32000}
    s.close()


def test_busy_agent_is_only_stopped_by_a_hard_cap(fake_pi, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "chatty")
    s = PiSession(spec(tmp_path, "busy"), pi_binary=str(fake_pi))
    s.open()
    res = s.prompt("go", 1.5, idle_timeout=0.8)                        # streaming every 0.2s: silence never trips
    assert res.timed_out and res.stopped_by == "limit"
    s.close()
