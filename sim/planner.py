"""The planner: opens milestones (epics), each a handful of tasks with dependencies among them.

Milestones arrive at random, about ``TASKING_MILESTONES_PER_MINUTE`` (10) a minute, at a rate that
rises and falls over ``CYCLE_MIN`` minutes (busy and quiet spells). Inside a milestone each task may depend on up to two earlier ones (blocks), and now
and then a milestone's first task depends on a task of the milestone before it. Tasks carry a
priority (1 high, 3 low), an estimate in minutes, and an area label."""

from __future__ import annotations

import math
import os
import random
import time

from .bd_client import Beads
from .common import log, main_guard, run

PER_MINUTE = float(os.environ.get("TASKING_MILESTONES_PER_MINUTE", "10"))
CYCLE_MIN, SWING = 10.0, 0.6        # busy and quiet spells, a cycle every 10 real minutes
AREAS = ["api", "ui", "billing", "search", "auth", "export", "infra", "docs"]
VERBS = ["Add", "Fix", "Refactor", "Test", "Document", "Speed up", "Migrate", "Remove"]
THINGS = ["the login form", "the export job", "retry logic", "the billing report", "the search index", "rate limits",
          "the settings page", "the audit log", "CSV import", "health checks", "the cache", "webhooks"]
THEMES = ["Self-serve onboarding", "Faster search", "Billing v2", "Audit readiness", "Bulk export", "Mobile layout",
          "SSO", "Usage limits", "Status page", "Data retention"]


def planner() -> None:
    b, st = Beads("planner"), {"n": 0, "last_tasks": []}

    def step() -> None:
        rate = PER_MINUTE * (1 + SWING * math.sin(2 * math.pi * (time.time() / 60) / CYCLE_MIN)) / 60   # per second
        if random.random() >= rate:                             # this step is 1 s long
            return
        st["n"] += 1
        epic = b.create(f"M{st['n']}: {random.choice(THEMES)}", issue_type="epic", priority=1)
        area, ids = random.choice(AREAS), []
        for i in range(random.randint(4, 9)):
            deps = random.sample(ids, k=min(len(ids), random.choice([0, 0, 1, 1, 2])))
            if i == 0 and st["last_tasks"] and random.random() < 0.25:
                deps.append(random.choice(st["last_tasks"]))    # across milestones
            t = b.create(f"{random.choice(VERBS)} {random.choice(THINGS)}", parent=epic["id"], depends_on=deps,
                         priority=random.choice([1, 2, 2, 3]), estimated_minutes=random.choice([2, 3, 5, 5, 8, 13]),
                         labels=[f"area:{area}"])
            ids.append(t["id"])
        st["last_tasks"] = ids
        log("planner", f"{epic['id']} {epic['title']}: {len(ids)} tasks")
    run("planner", step, 1.0)


def main() -> None:
    main_guard(planner)
