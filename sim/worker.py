"""A worker: takes the most urgent ready task, works it, and hands it to review.

It keeps its claim alive with heartbeats while it works. Now and then it finds more work and
files a follow-up task in the same milestone (work making work), and now and then it crashes
mid-task and is gone for a while: its lease goes stale and the release role reclaims the task."""

from __future__ import annotations

import os
import random
import time

from .bd_client import Beads, BeadsError
from .common import log, main_guard, work_seconds

FOLLOW_UP, CRASH = 0.08, 0.01
HEARTBEAT_S = 20.0


def worker(n: int) -> None:
    me = f"worker-{n}"
    b = Beads(me)
    skill = float(os.environ.get("TASKING_SKILL", str([1.0, 0.8, 1.3, 1.0, 0.9, 1.2][(n - 1) % 6])))   # <1 faster
    log(me, f"ready (skill {skill})")
    while True:
        try:
            ready = sorted(b.ready("task"), key=lambda i: (i.get("priority", 2), i.get("created_at", "")))
            task = next((t for t in (b.claim(i["id"]) for i in ready[:5]) if t), None)
            if task is None:
                time.sleep(0.5)
                continue
            secs = work_seconds(task.get("estimated_minutes") or 3, skill)
            log(me, f"works on {task['id']} ({task['title']}) for {secs:.0f}s")
            if random.random() < CRASH:
                log(me, f"crashes while working on {task['id']}")
                time.sleep(secs * 4)                            # gone: no heartbeats, the lease goes stale
                continue
            end = time.time() + secs
            while time.time() < end:                            # heartbeats only for work longer than HEARTBEAT_S
                time.sleep(min(HEARTBEAT_S, max(0.0, end - time.time())))
                if time.time() < end and not b.heartbeat(task["id"]):
                    log(me, f"lost {task['id']} (its lease was reclaimed)")
                    break
            else:
                if random.random() < FOLLOW_UP:
                    parent = (task.get("parent") or task.get("parent_id"))
                    f = b.create(f"Follow-up: {task['title']}", parent=parent, priority=2, estimated_minutes=random.choice([2, 3, 5]),
                                 labels=["follow-up"], description=f"Found while working on {task['id']}.")
                    log(me, f"filed follow-up {f['id']}")
                b.update(task["id"], {"status": "in_review", "assignee": ""}, status="in_progress", assignee=me)
                log(me, f"{task['id']} to review")
        except BeadsError as e:
            log(me, f"tracker said {e}")
            time.sleep(3)
        except Exception as e:  # noqa: BLE001 - a worker keeps going
            log(me, f"step failed: {type(e).__name__}: {e}")
            time.sleep(5)


def main(n: int) -> None:
    main_guard(lambda: worker(n))
