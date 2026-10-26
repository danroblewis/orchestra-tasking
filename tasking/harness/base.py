"""The harness interface: how Orchestra drives an agent session, whichever product runs it.

A *harness* is an agent runner (Pi, Claude Code, Codex, OpenCode). Orchestra needs four
things from one, and nothing else:

1. ``start(spec)`` opens a named, resumable session with a system prompt, a working
   directory, and a set of Orchestra tool groups.
2. ``session.prompt(text, timeout, on_event)`` runs one turn to completion and returns
   its final text; events arrive in the **normalized** vocabulary below so the loop and
   the UI never see harness-specific shapes.
3. ``session.session_file`` names the harness's own transcript, which is the only one
   Orchestra keeps (it never writes a parallel log); ``harness.read_transcript`` parses
   it into a common message list.
4. ``kill()`` / ``close()`` end a turn or the session.

Normalized events (dicts with ``type``):
  turn_start                 the prompt was accepted
  assistant_start            the model began a message
  token                      output is streaming (may be coalesced; used for liveness)
  tool_start {name, args}    a tool call began
  tool_end {name}            it finished
  assistant_end {text, tool_calls}   an assistant message completed
  settled {text}             the turn is over
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

OnEvent = Callable[[dict], None]


@dataclass
class TurnResult:
    text: str = ""
    settled: bool = False
    timed_out: bool = False
    stopped_by: str | None = None    # "silence" (no event for idle_timeout) or "limit" (the hard cap), when timed_out
    error: str | None = None
    tool_calls: list[str] = field(default_factory=list)


class _Deadlines:
    """Shared turn-ending rule for every harness: a turn ends when the agent settles, when
    nothing has arrived from the harness for `idle` seconds (the agent is presumed stuck),
    or, only if a hard cap is set, when `cap` seconds have passed in total. A busy agent is
    never cut off by the cap unless the project asked for one."""

    def __init__(self, cap: float | None, idle: float | None):
        now = time.time()
        self.hard = now + cap if cap else None
        self.idle = idle if idle else None
        self.last = now

    def touch(self) -> None:
        self.last = time.time()

    def expired(self) -> str | None:
        now = time.time()
        if self.hard is not None and now >= self.hard:
            return "limit"
        if self.idle is not None and now - self.last >= self.idle:
            return "silence"
        return None

    def wait(self) -> float:
        """How long to block for the next event: until the nearest deadline, at most 5s."""
        now = time.time()
        waits = [5.0]
        if self.hard is not None:
            waits.append(self.hard - now)
        if self.idle is not None:
            waits.append(self.last + self.idle - now)
        return max(0.1, min(waits))


@dataclass
class SessionSpec:
    """Everything a harness needs to open a session. ``env`` carries the ORCHESTRA_*
    variables the Orchestra tools read (repo, role, ticket, tool groups)."""

    session_id: str
    session_dir: Path          # Orchestra's per-project sessions directory
    cwd: Path
    system_prompt: str
    role: str
    tool_groups: tuple[str, ...]
    env: dict[str, str]
    log_path: Path             # harness stderr / diagnostics
    model: str | None = None
    provider: str | None = None
    thinking: str | None = None
    ticket_id: str = ""
    compact_tokens: int | None = None   # ask the harness to compact its own context past this many tokens, if it can


class AgentSession(ABC):
    """One named session. Thread-safe for one prompt at a time."""

    spec: SessionSpec
    session_file: str | None = None   # the harness's transcript, once known

    @property
    def session_id(self) -> str:
        return self.spec.session_id

    @property
    def harness_name(self) -> str:
        return "unknown"

    @abstractmethod
    def open(self) -> None:
        """Make the session exist and learn ``session_file``. Idempotent."""

    @abstractmethod
    def prompt(self, text: str, timeout: float | None, on_event: OnEvent | None = None,
               idle_timeout: float | None = None) -> TurnResult:
        """Run one turn. `timeout` is a hard cap in seconds (None or 0: none); `idle_timeout`
        ends the turn after that many seconds without any event from the harness."""
        ...

    def compact(self) -> dict | None:
        """Ask the harness to summarise the older part of this session's context in place,
        keeping the transcript. Returns the harness's result (tokens before and after) or
        None when this harness cannot compact; the loop then falls back to rotation."""
        return None

    @property
    @abstractmethod
    def pid(self) -> int | None:
        """A live OS process that proves this session is alive right now, or None."""

    @abstractmethod
    def alive(self) -> bool: ...

    @abstractmethod
    def kill(self) -> None:
        """Stop the running turn immediately (the operator's stop button)."""

    @abstractmethod
    def close(self) -> None:
        """Release the session's resources. The transcript stays; the session is resumable."""


class Harness(ABC):
    name: str = "unknown"

    @abstractmethod
    def start(self, spec: SessionSpec) -> AgentSession: ...

    @staticmethod
    @abstractmethod
    def read_transcript(session_file: str | Path) -> list[dict]:
        """Parse the harness's transcript into [{role, text, tool_calls, ts, ...}] where role
        is user | assistant | toolResult. ``ts`` is an ISO-8601 string or epoch seconds."""


def assistant_text(content) -> str:
    """Text of an assistant message whose content is a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""
