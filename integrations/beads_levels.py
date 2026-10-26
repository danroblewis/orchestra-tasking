"""Beads' counts that its journal does not carry, into Orchestra's metrics store every ``EVERY_S``
seconds: how many tasks are ready (``beads_ready{type}``) and how many open issues are blocked by a
dependency (``beads_blocked``). Labelled ``machine="beads"``. (How many issues are in each status
comes from the journal, with the transitions: beads_sync.)"""

from __future__ import annotations

import time

from orchestra.config import load_project
from orchestra.metrics import Point
from orchestra.metrics import for_project as metrics_for

from . import beads_api

EVERY_S = 2.0
STATUSES = ("open", "in_progress", "in_review", "closed")
TYPES = ("task", "epic")


def main() -> None:
    project = load_project()
    store = metrics_for(project)
    base = {"project": project.key, "machine": "beads"}
    print(f"{time.strftime('%H:%M:%S')} beads-levels: every {EVERY_S:g} s", flush=True)
    while True:
        t = time.time()
        try:
            pts = []
            pts.append(Point("beads_ready", float(beads_api.ready(type="task")), t, {**base, "type": "task"}))
            pts.append(Point("beads_blocked", float(beads_api.get("/stats").get("summary", {}).get("blocked_issues", 0)), t, base))
            store.write(pts)
        except OSError as e:
            print(f"{time.strftime('%H:%M:%S')} beads-levels: Beads did not answer: {e}", flush=True)
        time.sleep(max(0.2, EVERY_S - (time.time() - t)))


if __name__ == "__main__":
    main()
