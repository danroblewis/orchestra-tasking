"""Release: closes a milestone when every one of its tasks is closed, and reclaims tasks whose
worker's lease went stale (the worker died), so they go back to the ready queue."""

from __future__ import annotations

import os
import time

from .bd_client import Beads
from .common import log, main_guard, run

RECLAIM_EVERY_S, STALE = 30.0, os.environ.get("TASKING_STALE", "60s")


def release() -> None:
    b, st = Beads("release"), {"reclaimed_at": 0.0}

    def step() -> None:
        for epic in b.list(status="open", type="epic"):
            kids = b.list(parent=epic["id"])
            if kids and all(k["status"] == "closed" for k in kids):
                b.close(epic["id"], f"all {len(kids)} tasks done")
                log("release", f"milestone {epic['id']} done ({len(kids)} tasks)")
        if time.time() - st["reclaimed_at"] > RECLAIM_EVERY_S:
            st["reclaimed_at"] = time.time()
            r = b.bd("reclaim", "--older-than", STALE)
            if r.stdout.strip() and "no stale" not in r.stdout.lower():
                log("release", "reclaim: " + r.stdout.strip().splitlines()[-1][:200])
    run("release", step, 2.0)


def main() -> None:
    main_guard(release)
