"""Beads' event journal into Orchestra: every change to an issue, as an event on Orchestra's bus,
and every change of status as a counter in its metrics store.

Each journal record (seq, ts, op, issue_id, actor, the issue as it is after the change) becomes:

- an event ``beads.issue.<op>`` (create, update, close, reopen, dep_add, comment…) from the
  actor, as a machine and instance (``worker-3`` is ``worker/3``), with Beads' own time, the issue
  as subject, and an id stable across re-imports (``beads:<project>:<seq>``). Its data says what
  changed: the status before and after, the issue's type, title and parent;
- when the status changed, one more on ``beads_issue_transitions_total{from, to, type}``, credited
  to the actor's role (``machine="worker"``), written at the time of the change; a new issue counts
  on ``beads_issues_created_total{type}``;
- the number of issues in each status, ``beads_issues{status, type}`` (``machine="beads"``), as it
  stands after the change, at the time of the change. Taken from the same records as the counters,
  so levels and flows line up exactly (a level polled now and then would be off by whatever moved
  between the poll and the step it lands in).

The status before is what the previous record said; the sync keeps each issue's last status and
its place in the journal in ``run/beads-sync.json``, so it carries on where it stopped (from the
start of the journal the first time). Replaying the journal again is safe: an event is published
only if the bus does not already hold it (the last Beads event on the bus is found by its id when
the sync starts), and the counters and levels are rewritten at their own times, with the same values."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from orchestra.config import load_project
from orchestra.events import Events
from orchestra.metrics import Point
from orchestra.metrics import for_project as metrics_for

from . import beads_api

STATE = Path(__file__).resolve().parent.parent / "run" / "beads-sync.json"


def actor_source(actor: str | None) -> tuple[str, str]:
    """``worker-3`` → (``worker/3``, ``worker``); ``planner`` → (``planner``, ``planner``). A
    record with no actor is Beads' own doing (it updates issues itself, e.g. when a dependency
    changes whether one is blocked): the source is ``beads``."""
    a = actor or "beads"
    m = re.fullmatch(r"(.+?)-(\d+)", a)
    return (f"{m.group(1)}/{m.group(2)}", m.group(1)) if m else (a, a)


def main() -> None:
    project = load_project()
    bus = Events.for_project(project, source="beads-sync")
    store = metrics_for(project)
    pid = beads_api.context().get("project_id", "beads")
    st = json.loads(STATE.read_text()) if STATE.exists() else {"seq": 0, "status": {}, "counts": {}}
    on_bus = last_on_bus(bus, pid)
    print(f"{time.strftime('%H:%M:%S')} beads-sync: the bus holds Beads events to seq {on_bus}", flush=True)
    st.setdefault("kinds", {})
    print(f"{time.strftime('%H:%M:%S')} beads-sync: from seq {st['seq']} (journal of {pid})", flush=True)
    while True:
        try:
            records, head = beads_api.events(st["seq"])
        except OSError as e:
            print(f"{time.strftime('%H:%M:%S')} beads-sync: Beads did not answer: {e}", flush=True)
            time.sleep(3)
            continue
        points, levels = [], {}
        for r in records:
            issue = r.get("issue") or {}
            iid, op, kind = r.get("issue_id"), r.get("op", "update"), issue.get("issue_type", "task")
            before, after = st["status"].get(iid), issue.get("status")
            source, role = actor_source(r.get("actor"))
            t = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp()
            # Beads stamps to the second, and many changes share one: keep their journal order within
            # it (a millisecond apart), so a level written twice in a second reads back as the later
            if int(t) != st.get("sec"):
                st["sec"], st["idx"] = int(t), 0
            st["idx"] = min(999, st.get("idx", 0) + 1)
            t += st["idx"] / 1000.0
            if r["seq"] > on_bus:                               # not on the bus yet
                bus.publish(f"beads.issue.{op}", {"seq": r["seq"], "issue_id": iid, "issue_type": kind, "title": issue.get("title"),
                                                  "from": before, "to": after, "parent": issue.get("parent")},
                            source=source, subject=iid, time=r["ts"], id=f"beads:{pid}:{r['seq']}")
            if op == "create":
                points.append(_count(st, "beads_issues_created_total", {"type": kind, "machine": role}, t, project.key))
            if before and after and before != after:
                points.append(_count(st, "beads_issue_transitions_total", {"from": before, "to": after, "type": kind, "machine": role}, t, project.key))
            if after:
                st["status"][iid] = after
                st["kinds"][iid] = kind
                for s in {before, after} - {None}:              # the levels this record moved, as they now stand
                    n = sum(1 for i, x in st["status"].items() if x == s and st["kinds"].get(i) == kind)
                    levels[(s, kind, t)] = Point("beads_issues", float(n), t, {"project": project.key, "machine": "beads", "status": s, "type": kind})
            st["seq"] = r["seq"]
        points += list(levels.values())
        if points:
            store.write(points)
        if records:
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(st))
            os.replace(tmp, STATE)
            print(f"{time.strftime('%H:%M:%S')} beads-sync: {len(records)} records to seq {st['seq']} (head {head})", flush=True)
        if st["seq"] >= head:
            time.sleep(1.0)


def last_on_bus(bus: Events, pid: str) -> int:
    """The highest journal seq already published to the bus (0 if none), by the events' ids."""
    prefix, best, since = f"beads:{pid}:", 0, 0
    end = bus.end()
    while since < end:
        evs, since = bus.read(since, limit=min(5000, end - since))
        for ev in evs:
            if str(ev.get("id", "")).startswith(prefix):
                best = max(best, int(ev["id"][len(prefix):]))
        if not evs:
            break
    return best


def _count(st: dict, name: str, labels: dict, t: float, project: str) -> Point:
    key = name + json.dumps(labels, sort_keys=True)
    st["counts"][key] = st["counts"].get(key, 0) + 1
    return Point(name, float(st["counts"][key]), t, {"project": project, **labels})


if __name__ == "__main__":
    main()
