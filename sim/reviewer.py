"""A reviewer: approves finished work or sends it back. Each reviewer takes its share of the
tasks in review (by a hash of the id), so two never review the same task. A rejected task goes
back to the ready queue with a comment, to be reworked."""

from __future__ import annotations

import os
import random
import time
import zlib

from .beads import Beads
from .common import log, main_guard, run, work_seconds

APPROVE = float(os.environ.get("TASKING_APPROVE", "0.8"))


def reviewer(n: int, of: int) -> None:
    me = f"reviewer-{n}"
    b = Beads(me)

    def step() -> None:
        mine = [i for i in b.list(status="in_review", type="task") if zlib.crc32(i["id"].encode()) % of == n - 1]
        for t in sorted(mine, key=lambda i: i.get("priority", 2))[:1]:
            time.sleep(work_seconds((t.get("estimated_minutes") or 3) * 0.25))
            if random.random() < APPROVE:
                b.close(t["id"], "approved in review")
                log(me, f"approved {t['id']}")
            else:
                b.update(t["id"], {"status": "open"}, status="in_review")
                log(me, f"sent {t['id']} back")
    run(me, step, 2.0)


def main(n: int) -> None:
    main_guard(lambda: reviewer(n, int(os.environ.get("TASKING_REVIEWERS", "2"))))
