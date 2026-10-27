"""A reviewer that is a program: it checks a task's result against the ticket's acceptance text.

It fills the task system's ``reviewer`` role by speaking the assignment protocol on Orchestra's
bus (orchestra-tasking: tasking/assignments.py), using nothing but Orchestra's library. On an
offer of a review (``review.requested`` from the worker that did the work, or ``task.assigned``
from the task system's queue) it claims it and reads the bus until its claim is there: it goes
on only if no other claim of the offer came first. It looks in the ticket's workspace and
approves (``review.approved``, for the task system's gate) when RESULT.md is there and every
criterion it can check holds; otherwise it asks the worker that did the work for changes
(``changes.requested``, addressed to it), naming each criterion that failed. A criterion is a
piece of the acceptance text:

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
import uuid
from pathlib import Path

from orchestra.config import load_project
from orchestra.events import Events

ROLE = "reviewer"
ASSIGNED, ACCEPTED = "task.assigned", "task.accepted"
REVIEW_REQUESTED, CHANGES_REQUESTED, APPROVED = "review.requested", "changes.requested", "review.approved"
OFFERS = (ASSIGNED, REVIEW_REQUESTED)
FILES, TEXTS = re.compile(r"`([^`\n]+)`"), re.compile(r'"([^"\n]+)"')


def main(instance: str) -> None:
    me = f"{ROLE}/{instance}"
    events = Events.for_project(load_project(), source=me)
    events.publish("orchestra.machine.started", {"mode": "continuous", "kind": "program", "listens_for": list(OFFERS),
                                                 **({"log": os.environ["ORCHESTRA_MACHINE_LOG"]} if os.environ.get("ORCHESTRA_MACHINE_LOG") else {})})
    print(f"{me}: waiting for {ROLE} assignments", flush=True)
    offset = events.end()
    while True:
        start = offset                                   # the offers below are after this: a claim is read back from here
        evs, offset = events.after(offset)
        for ev in evs:
            d = ev.get("data") or {}
            if ev.get("type") in OFFERS and d.get("role") == ROLE and d.get("to") in (None, me):
                review(events, me, {**d, "id": ev.get("id")}, start)
        time.sleep(0.5)


def first_claim(evs: list[dict], offer: dict) -> str | None:
    """The source of the first valid claim of ``offer`` among ``evs`` (envelopes, in bus order)."""
    for ev in evs:
        d, src = ev.get("data") or {}, ev.get("source")
        if ev.get("type") == ACCEPTED and d.get("assignment") == offer["assignment"] and src \
                and d.get("instance") in (None, src) and offer.get("to") in (None, src):
            return src
    return None


def review(events: Events, me: str, offer: dict, since: int) -> None:
    tid, brief, folder = offer["ticket"], offer.get("brief") or {}, Path(offer["workspace"])
    cause = {"causationid": offer["id"]} if offer.get("id") else {}

    claim = events.publish(ACCEPTED, {"assignment": offer["assignment"], "ticket": tid, "role": ROLE, "instance": me}, subject=tid, **cause)
    while True:                                          # its claim, read back: was it the first?
        evs, _ = events.after(since)
        ids = [e.get("id") for e in evs]
        if claim["id"] in ids:
            break
        time.sleep(0.2)
    if first_claim(evs[:ids.index(claim["id"]) + 1], offer) != me:
        print(f"{me}: {tid}: another reviewer's claim came first; not mine", flush=True)
        return
    report = verdict(brief.get("acceptance") or "", folder)
    review_ = offer.get("review") or {}
    if report["verdict"] == "APPROVED":
        events.publish(APPROVED, {"ends": offer["assignment"], "ticket": tid, "role": ROLE, "summary": report["summary"],
                                  "commit": review_.get("commit")}, subject=tid, **cause)
    else:
        b = dict(brief)                                   # as the ticket will read once the worker takes it back
        b.update(feedback=report["feedback"], rejected_by=ROLE, worker_turns=(b.get("worker_turns") or 0) + 1,
                 review_rounds=(b.get("review_rounds") or 0) + 1)
        keep = {k: offer.get(k) for k in ("workspace", "branch", "base", "lease_s", "unit_command") if k in offer}
        events.publish(CHANGES_REQUESTED, {**keep, "assignment": f"{tid}.worker.{uuid.uuid4().hex[:8]}", "ticket": tid, "role": "worker",
                                           "to": review_.get("worker"), "ends": offer["assignment"], "brief": b,
                                           "summary": report["summary"], "feedback": report["feedback"], "commit": review_.get("commit")},
                       subject=tid, **cause)
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
