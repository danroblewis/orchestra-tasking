"""The task system's measurements, served in the Prometheus text format and read by an instrument
(rule 7: the metrics store is filled by instruments; the scheduler writes none itself). Named for
what they measure (Orchestra's discovery decides what is a stock and what is a flow):

- ``tasks_tickets{state}``: tickets in each state, every state;
- ``tasks_tickets_created_total{by}``: tickets created;
- ``tasks_ticket_transitions_total{from, to, by}``: state changes;
- ``tasks_worktrees{state}``: with reused worktrees (tasking/pool.py), the pool's worktrees
  ``in_use`` and ``free``, and the turns ``waiting`` for one because the pool is full.

``by`` is the machine whose act or responsibility moved the ticket: the ``worker`` that asked for
a review or whose turn ran out, the ``reviewer`` whose verdict it was, the ``merge_gate`` that
landed it, the planner machine that asked for it or cancelled it. Never ``task_scheduler``: the
scheduler is no machine, only the daemon that stands in for each machine's own scheduler
(tasking/events.py). Discovery puts a label's differing values into a flow's name, so a flow reads
"ticket transitions (IN_PROGRESS, READY_FOR_REVIEW, worker)".

Where they come from: the counters are cumulative across processes and restarts, kept with the
tickets in the record (``metric_counts``) under the store's lock and moved in the same write as
the change they count, so a level and the counters read from one record balance exactly. The
plugin declares the instrument (``Plugin.collectors``, tasking/web.py): ``orchestra run`` runs
``tasking metrics`` every discovery step, which prints them from the record, and labels every
point ``instrument=tasks.tickets``. The same text is served at the plugin's ``/metrics``
(``/api/plugins/tasks/metrics``) for anyone to read.

Until 2026-10-27 the scheduler wrote these points itself, with ``machine="task_scheduler"`` on
every series. That label is gone (the scheduler is no machine), and ``instrument`` is new; the
series' names are kept, so a store's history splits at the upgrade by label, not by name. Counters
kept before then carry ``machine`` in their key and are summed without it."""

from __future__ import annotations

from .model import State

CREATED, TRANSITIONS = "tasks_tickets_created_total", "tasks_ticket_transitions_total"
LEVELS, WORKTREES = "tasks_tickets", "tasks_worktrees"
HELP = {LEVELS: ("gauge", "tickets in each state"),
        CREATED: ("counter", "tickets created, by the machine that asked for them"),
        TRANSITIONS: ("counter", "ticket state changes, by the machine whose act or responsibility moved the ticket"),
        WORKTREES: ("gauge", "the worktree pool: worktrees in use and free, and turns waiting for one")}
_KEYS = {"created": CREATED, "transition": TRANSITIONS}


def count(counts: dict, kind: str, labels: dict) -> None:
    """Add one to a cumulative counter kept in ``counts`` (a dict saved with the tickets, under the
    store's lock). ``kind``: created | transition."""
    key = kind + "|" + "|".join(f"{k}={v}" for k, v in sorted(labels.items()))
    counts[key] = counts.get(key, 0) + 1


def counters(counts: dict) -> dict[tuple[str, tuple], int]:
    """The kept counters by (series name, labels), a ``machine`` kept before 2026-10-27 left out
    and its counts summed."""
    out: dict[tuple[str, tuple], int] = {}
    for key, n in counts.items():
        kind, *parts = key.split("|")
        if kind not in _KEYS:
            continue
        labels = tuple(sorted((k, v) for k, _, v in (p.partition("=") for p in parts) if k != "machine"))
        out[(_KEYS[kind], labels)] = out.get((_KEYS[kind], labels), 0) + int(n)
    return out


def levels(tickets: dict) -> dict[str, int]:
    """Every state's count, as the tickets (id -> record) stand."""
    by_state = {s.value: 0 for s in State}
    for d in tickets.values():
        by_state[d["state"]] = by_state.get(d["state"], 0) + 1
    return by_state


def _line(name: str, labels: tuple | dict, value: float) -> str:
    items = labels.items() if isinstance(labels, dict) else labels
    inner = ",".join(f'{k}="{_escape(str(v))}"' for k, v in items)
    return f"{name}{{{inner}}} {value:g}" if inner else f"{name} {value:g}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def exposition(tickets: dict, counts: dict, pool: tuple[int, int, int] | None = None) -> str:
    """The measurements as Prometheus text: the levels, the counters, and, when the pool is used
    (``pool``: in use, free, waiting), the worktrees."""
    lines: list[str] = []
    series: dict[str, list[str]] = {LEVELS: [_line(LEVELS, {"state": s}, n) for s, n in levels(tickets).items()]}
    for (name, labels), n in sorted(counters(counts).items()):
        series.setdefault(name, []).append(_line(name, labels, n))
    if pool is not None:
        series[WORKTREES] = [_line(WORKTREES, {"state": s}, n) for s, n in zip(("in_use", "free", "waiting"), pool, strict=True)]
    for name, rows in series.items():
        kind, text = HELP[name]
        lines += [f"# HELP {name} {text}", f"# TYPE {name} {kind}", *rows]
    return "\n".join(lines) + "\n"
