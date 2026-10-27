"""A reviewer that is a program: it checks a task's result against the ticket's acceptance text.

It fills the task system's ``reviewer`` role by speaking the assignment protocol on Orchestra's
bus (orchestra-tasking: tasking/assignments.py), using nothing but Orchestra's library. On
``task.assigned`` for a reviewer it accepts, looks in the ticket's workspace, and approves when
RESULT.md is there and every criterion it can check holds; otherwise it rejects, naming each
criterion that failed. A criterion is a piece of the acceptance text:

- `` `name` ``: a file named so exists in the workspace;
- ``"text"``: RESULT.md contains the text.

Anything else in the acceptance text is for a person to judge; this reviewer says it did not
check it. A review takes a moment, well within the lease, so it sends no heartbeat.

    python machines/check_reviewer.py [instance]    # the machine ``reviewer``, instance 1 by default
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from orchestra.config import load_project
from orchestra.events import Events

ROLE = "reviewer"
ASSIGNED, ACCEPTED, REPORTED = "task.assigned", "task.accepted", "task.reported"
FILES, TEXTS = re.compile(r"`([^`\n]+)`"), re.compile(r'"([^"\n]+)"')


def main(instance: str) -> None:
    me = f"{ROLE}/{instance}"
    events = Events.for_project(load_project(), source=me)
    events.publish("orchestra.machine.started", {"mode": "continuous", "kind": "program", "listens_for": [ASSIGNED],
                                                 **({"log": os.environ["ORCHESTRA_MACHINE_LOG"]} if os.environ.get("ORCHESTRA_MACHINE_LOG") else {})})
    print(f"{me}: waiting for {ROLE} assignments", flush=True)
    offset = events.end()
    while True:
        evs, offset = events.after(offset)
        for ev in evs:
            d = ev.get("data") or {}
            if ev.get("type") == ASSIGNED and d.get("role") == ROLE:
                review(events, me, d)
        time.sleep(0.5)


def review(events: Events, me: str, offer: dict) -> None:
    tid, brief, folder = offer["ticket"], offer.get("brief") or {}, Path(offer["workspace"])

    def say(kind: str, **data) -> None:
        events.publish(kind, {"assignment": offer["assignment"], "ticket": tid, "role": ROLE, **data}, subject=tid)

    say(ACCEPTED, instance=me)
    report = verdict(brief.get("acceptance") or "", folder)
    say(REPORTED, report=report, failed=False, text="")
    print(f"{me}: {tid} {report['verdict']}", flush=True)


def verdict(acceptance: str, folder: Path) -> dict:
    result = folder / "RESULT.md"
    if not result.exists():
        return {"verdict": "REJECTED", "summary": "no RESULT.md", "feedback": "The workspace has no RESULT.md: write what was done there."}
    text = result.read_text(encoding="utf-8", errors="replace")
    failed = [f"the file `{name}` is missing from the workspace" for name in FILES.findall(acceptance) if not (folder / name).exists()]
    failed += [f'RESULT.md does not contain "{line}"' for line in TEXTS.findall(acceptance) if line not in text]
    checked = len(FILES.findall(acceptance)) + len(TEXTS.findall(acceptance))
    unchecked = [ln.strip() for ln in acceptance.splitlines() if ln.strip() and not FILES.search(ln) and not TEXTS.search(ln)]
    note = f" Not checked (for a person to judge): {'; '.join(unchecked)}." if unchecked else ""
    if failed:
        return {"verdict": "REJECTED", "summary": f"{len(failed)} of {checked} criteria failed",
                "feedback": "\n".join(f"- {f}" for f in failed) + note}
    return {"verdict": "APPROVED", "summary": f"RESULT.md is there and all {checked} checkable criteria hold.{note}"}


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "1")
    except KeyboardInterrupt:
        pass
