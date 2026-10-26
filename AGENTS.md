# Rules for working on the task system

Read `SPEC.md` first. It is the design (lifted from Orchestra; see its note). This file is the short list of rules
that must not be broken without updating the spec and saying so in the commit.

## Invariants

1. **Only Orchestra changes ticket state.** Agents report through their tools;
   the loop applies the transition table in `SPEC.md` section 3. Never add a
   code path where an agent's output is translated into a different state.
2. **No zombies.** A ticket is `IN_PROGRESS`, `IN_REVIEW`, or `MERGING` only
   while a live process owns it. When the process stops, the ticket moves to a
   waiting state in the same code path.
3. **One worktree per ticket.** Created when the ticket first starts, reused
   by every agent that touches the ticket, deleted when the ticket is `DONE`
   or `CANCELLED`. Workers and reviewers run with their working directory set
   to it. Nothing is ever written inside the user's checkout except
   `orchestra.toml` and `.orchestra/` (roles, agents, teams, and team spaces,
   written only through Orchestra's stores; never by the merge gate).
4. **Merging and deploying are programmatic.** No agent runs `git merge`,
   `git push`, touches the base branch, or starts staging. Only the gate
   lands code, and only the gate's success promotes a build to staging. The
   gate is not valid without a build artifact and acceptance tests that run
   against it.
5. **State lives in `~/.orchestrator/<key>/tasks/`, except the event log, which
   lives in Orchestra's event bus.** Business logic goes through the
   `TicketBackend` interface and the runtime store. No module reads or writes
   those JSON files directly except `tasking/store/`. Events go through
   `tasking.events.Events` (over Orchestra's `orchestra.events.Events` and `orchestra.bus.EventBus`) (a JSONL file in
   the state directory by default; Redis Streams, a RabbitMQ stream or Kafka
   by `[events] url`); nothing else touches a backend directly. Orchestra's
   streams are named `orchestra.<key>.events` and never share a topic with
   anyone else's.
6. **The harness's session file is the only transcript.** Do not write a
   parallel log of agent messages. Pi and Claude Code each keep their own;
   `tasking.harness.read_transcript` parses whichever wrote it.
7. **Humans talk only to the Supervisor.** There is no human path to create or
   edit tickets or milestones; the CLI and UI are read-only for them except for
   sending a message to the Supervisor (`tasking chat`, or the box in its
   transcript drawer) and three operator controls: pausing new workers,
   stopping a running agent, and the ceiling on concurrent workers and
   reviewers (the Coordinator's dial sits under it). Humans never prompt the Coordinator, Pipeline
   Manager, workers, or reviewers.
8. **Every ticket has a milestone, and closed milestones are final.** A
   milestone closes only when all its tickets are terminal, can never reopen,
   and can never receive tickets. The store enforces this, not the agents.
9. **Roles, agents, and teams are data behind stores.** A role is a job
   description with no runtime; an agent is an instance of roles in a team
   and is what runs; a team has one lead, responsibilities, access, and a
   space. Built-ins live in `tasking/agents/roles.py` and `tasking/org.py`;
   custom ones come from the stores (default: the repository's `.orchestra/`).
   The loop treats built-in and custom the same way. Do not special-case any
   of them in the loop, and do not read their files outside `tasking/roles.py`
   and `tasking/org.py`.
10. **One package, no path hacks.** Everything importable is under
   `tasking/`. No `sys.path` manipulation. Orchestra is a dependency (`../orchestra_2`,
   editable); the task system uses its bus, metrics store and plugin interface and nothing else.

## Harness rules

- **Nothing outside `tasking/harness/` knows which product runs an agent.**
  The loop, CLI, web app, and store use `AgentSession`, normalized events,
  and `read_transcript`. A harness-specific branch anywhere else is a bug.
- **Orchestra's tools are defined once**, in `tasking/harness/tools.py`.
  The Pi extension and the MCP server both read that catalogue; never add a
  tool to one harness only.
- **Every harness has a fake** in `tests/e2e/` that speaks its real protocol,
  and the loop e2e tests run against every fake.

## Harness facts the code depends on

- Pi: no MCP, no inbound HTTP, no scheduler; tools via the extension. `pi
  --mode rpc` reads one JSON command per line on stdin and writes events on
  stdout; `agent_settled` is the completion signal; `message_end` with
  `role: assistant` is the final message. Sessions are JSONL files placed by
  `--session-dir`/`--session-id`. Docs:
  `~/.pi/agent/install/releases/<version>/node_modules/@earendil-works/pi-coding-agent/docs/`.
- Claude Code: `claude -p` is one turn per process; `--output-format
  stream-json` gives `system/init`, `stream_event`, `assistant`, `user`
  (tool results), `result`. `--session-id` names a new session,
  `--resume <uuid>` continues it from the same cwd. Transcripts live under
  `$CLAUDE_CONFIG_DIR` (default `~/.claude`) at `projects/<cwd with / as
  ->/<uuid>.jsonl`. Tools come from MCP servers in `--mcp-config`; MCP
  tools are deferred behind `ToolSearch`, which the model calls itself.
- Codex: `codex exec --json` is one turn per process; the prompt is `-` on
  stdin (a positional prompt with an open stdin hangs on "Reading additional
  input from stdin"). Events: `thread.started` (carries the thread id),
  `turn.started`, `item.started`/`item.completed` (whole items, no token
  stream), `turn.completed`, `turn.failed`, `error`. Global flags go before
  `resume <thread_id>`. Config comes from `-c key=<TOML value>`; MCP servers
  are `mcp_servers.<name>={command, args, env, required}`; the role prompt
  is `developer_instructions`. Rollouts live under `$CODEX_HOME/sessions/`
  (`response_item` lines hold the messages and function calls, `event_msg`
  `McpToolCall` items hold MCP calls). MCP tools reach the model as a
  `namespace` tool, which only OpenAI's own API understands.
- Timers live in the loop for every harness.

## Conventions

- Python 3.12+, `uv` for everything, `pytest` for tests, `ruff` for lint and
  format.
- `tests/unit` is fast and pure. `tests/e2e` drives the real loop, store, git,
  and gate with `tests/e2e/fake_pi.py`, which speaks Pi's RPC protocol; extend
  the fake rather than mocking the loop. `tests/acceptance` runs against the
  built wheel started by `scripts/acceptance_target.py` and skips unless
  `ORCHESTRA_TARGET_URL` is set. Tests that need a real model are opt-in via
  `ORCHESTRA_ACCEPTANCE=1` and never required for the suite to pass;
  `tests/acceptance/test_real_flow.py` runs the whole flow once per harness
  with real models and starts its own targets.
- Keep modules small and single-purpose. If a file needs a section divider
  comment, it should be two files.
