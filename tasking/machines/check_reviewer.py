"""A reviewer that is a program: it checks a ticket's result, a task's against the acceptance
text, code's by running a check command on the commit.

It fills the task system's ``reviewer`` role by speaking the assignment protocol on Orchestra's
bus (orchestra-tasking: tasking/assignments.py), using nothing but Orchestra's library. On an
offer of a review (``review.requested`` from the worker that did the work, or ``task.assigned``
from the task system's queue) it claims it and reads the bus until its claim is there: it goes
on only if no other claim of the offer came first. It approves (``review.approved``, for the
task system's gate) when every check holds; otherwise it asks the worker that did the work for
changes (``changes.requested``, addressed to it), naming what failed.

- a task (the offer names no branch): RESULT.md must be in the ticket's workspace, and every
  criterion it can read in the acceptance text must hold: `` `name` ``, a file named so exists in
  the workspace; ``"text"``, RESULT.md contains the text. Anything else in the acceptance text is
  for a person to judge; this reviewer says it did not check it.
- code (the offer names a branch): the workspace is the ticket's worktree, which must be at the
  commit the worker asked to have reviewed. The check is ``--check`` if given, else the offer's
  ``unit_command`` (the project's first ``[[gate.unit]]`` command), run there with
  ``ORCHESTRA_REPO`` naming the worktree, as the merge gate runs it; exit 0 approves. With neither,
  there is nothing for a program to check: it approves and says so, and the gate decides. The
  acceptance text is for a person or an AI reviewer; this one does not read it in code.

While a check runs it says it is still working, six times a lease, like the shell worker.

    python -m tasking.machines.check_reviewer [instance] [--check COMMAND]   # the machine ``reviewer``, instance 1 by default

It ships in the task system's package so that a project installs it rather than copying it; it
imports nothing from that package, so it stays the example of a machine written with Orchestra's
library alone.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from orchestra.config import load_project
from orchestra.events import Events

ROLE = "reviewer"
ASSIGNED, ACCEPTED, WORKING = "task.assigned", "task.accepted", "task.working"
REVIEW_REQUESTED, CHANGES_REQUESTED, APPROVED = "review.requested", "changes.requested", "review.approved"
OFFERS = (ASSIGNED, REVIEW_REQUESTED)
FILES, TEXTS = re.compile(r"`([^`\n]+)`"), re.compile(r'"([^"\n]+)"')
CHECK_TIMEOUT_S = 900


def main(instance: str, check: str | None) -> None:
    me = f"{ROLE}/{instance}"
    events = Events.for_project(load_project(), source=me)
    events.publish("orchestra.machine.started", {"mode": "continuous", "kind": "program", "listens_for": list(OFFERS),
                                                 **({"log": os.environ["ORCHESTRA_MACHINE_LOG"]} if os.environ.get("ORCHESTRA_MACHINE_LOG") else {})})
    print(f"{me}: waiting for {ROLE} assignments" + (f"; code is checked with: {check}" if check else ""), flush=True)
    offset = events.end()
    while True:
        start = offset                                   # the offers below are after this: a claim is read back from here
        evs, offset = events.after(offset)
        for ev in evs:
            d = ev.get("data") or {}
            if ev.get("type") in OFFERS and d.get("role") == ROLE and d.get("to") in (None, me):
                review(events, me, {**d, "id": ev.get("id")}, start, check)
        time.sleep(0.5)


def first_claim(evs: list[dict], offer: dict) -> str | None:
    """The source of the first valid claim of ``offer`` among ``evs`` (envelopes, in bus order)."""
    for ev in evs:
        d, src = ev.get("data") or {}, ev.get("source")
        if ev.get("type") == ACCEPTED and d.get("assignment") == offer["assignment"] and src \
                and d.get("instance") in (None, src) and offer.get("to") in (None, src):
            return src
    return None


def review(events: Events, me: str, offer: dict, since: int, check: str | None = None) -> None:
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
    review_ = offer.get("review") or {}
    if offer.get("branch") is None:
        report = verdict(brief.get("acceptance") or "", folder)
    else:
        over = threading.Event()
        threading.Thread(target=heartbeat, args=(events, offer, over, cause), daemon=True).start()
        try:
            report = code_verdict(folder, review_.get("commit"), check or offer.get("unit_command"))
        finally:
            over.set()
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


def heartbeat(events: Events, offer: dict, over: threading.Event, cause: dict) -> None:
    """Six heartbeats a lease (the offer's ``lease_s``) until ``over`` is set."""
    while not over.wait(float(offer.get("lease_s") or 180) / 6):
        events.publish(WORKING, {"assignment": offer["assignment"], "ticket": offer["ticket"], "role": ROLE, "text": "running the check"},
                       subject=offer["ticket"], **cause)


def verdict(acceptance: str, folder: Path) -> dict:
    """A task: RESULT.md, and the acceptance text's files and lines."""
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


def code_verdict(worktree: Path, commit: str | None, check: str | None) -> dict:
    """Code: the worktree at the commit to review, and ``check`` passing there."""
    head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    if commit and head != commit:
        return {"verdict": "REJECTED", "summary": "the worktree is not at the commit to review",
                "feedback": f"The worktree is at {head[:10] or 'no commit'}, not at {commit[:10]}, the commit asked to be reviewed: "
                            "leave the worktree at the commit you ask to have reviewed."}
    if not check:
        return {"verdict": "APPROVED", "summary": "No check command (neither --check nor the gate's unit command) to run here; "
                                                  "approved for the merge gate, which runs its own stages."}
    env = {**os.environ, "ORCHESTRA_REPO": str(worktree)}
    try:
        r = subprocess.run(["bash", "-c", check], cwd=worktree, env=env, capture_output=True, text=True, timeout=CHECK_TIMEOUT_S, check=False)
        rc, out = r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        rc, out = None, f"(stopped after {CHECK_TIMEOUT_S} s)"
    tail = out.strip()[-3000:] or "(nothing)"
    at = f" on {commit[:10]}" if commit else ""
    if rc == 0:
        return {"verdict": "APPROVED", "summary": f"`{check}` passed{at}.\n\n```\n{tail[-1500:]}\n```"}
    return {"verdict": "REJECTED", "summary": f"`{check}` failed{at} (exit {rc})",
            "feedback": f"`{check}` failed in the worktree{at} (exit {rc}); its output ends:\n\n```\n{tail}\n```"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("instance", nargs="?", default="1")
    ap.add_argument("--check", help="code: the command that must pass in the ticket's worktree (default: the offer's unit_command)")
    a = ap.parse_args()
    try:
        main(a.instance, a.check)
    except KeyboardInterrupt:
        pass
