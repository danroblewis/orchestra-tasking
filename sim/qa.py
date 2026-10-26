"""QA: finds defects that escaped review. Each task closed in the last few minutes gets one
look; a share of them are reopened as escaped defects and go back through the queue."""

from __future__ import annotations

import os
import random

from .bd_client import Beads
from .common import log, main_guard, run

ESCAPED = float(os.environ.get("TASKING_ESCAPED", "0.06"))


def qa() -> None:
    b, seen = Beads("qa"), set()

    def step() -> None:
        for t in b.list(status="closed", type="task"):
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            if random.random() < ESCAPED:
                b.reopen(t["id"], "escaped defect found by QA")
                seen.discard(t["id"])                           # it will be closed again, and looked at again
                log("qa", f"reopened {t['id']}")
    run("qa", step, 3.0)


def main() -> None:
    main_guard(qa)
