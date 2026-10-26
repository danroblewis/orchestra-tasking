"""What every role shares: the clock (TASKING_SPEED: how many simulated minutes pass per real
minute), logging to stdout, and a loop that survives the tracker being briefly unavailable."""

from __future__ import annotations

import os
import random
import sys
import time

SPEED = float(os.environ.get("TASKING_SPEED", "120"))    # simulated minutes per real minute: 1 estimated minute takes 60/SPEED s


def log(who: str, msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {who}: {msg}", flush=True)


def work_seconds(estimated_minutes: float, factor: float = 1.0) -> float:
    return max(1.0, estimated_minutes * 60.0 / SPEED * factor * random.lognormvariate(0, 0.35))


def run(who: str, step, every: float) -> None:
    """``step()`` forever, every ``every`` seconds; an error is logged and tried again."""
    while True:
        try:
            step()
        except Exception as e:  # noqa: BLE001 - a role keeps going
            log(who, f"step failed: {type(e).__name__}: {e}")
            time.sleep(min(10.0, every * 3))
        time.sleep(every)


def main_guard(fn) -> None:
    try:
        fn()
    except KeyboardInterrupt:
        sys.exit(0)
