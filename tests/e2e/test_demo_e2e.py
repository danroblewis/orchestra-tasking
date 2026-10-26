"""The demo, end to end through a real Orchestra: the template copied to a temporary directory,
`orchestra run` and `orchestra web` on a free port, `tasking run` and the two machines as
processes (demo/run.py's own functions), the tickets created as a planner would (requests over
the bus), and the rest read over HTTP: the task system's API, Orchestra's /api/machines, and the
Tickets page executed in a headless Chrome when one is at hand."""

import json
import os
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from demo.run import get, make_project, plan, start, stop, wait_for, wait_until

API = "/api/plugins/tasks"
CHROME = os.environ.get("CHROME") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """The demo running against a temporary home; every process is stopped afterwards, whatever
    the test did."""
    home = tmp_path / "home"
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(home))
    target = make_project(tmp_path / "project")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    procs = start(target, port, env={"ORCHESTRATOR_HOME": str(home)})
    try:
        wait_for(url, procs)
        yield url, target
    finally:
        stop(procs)
        assert all(p.poll() is not None for p in procs.values())


def states(url: str) -> dict[str, str]:
    return {t["id"]: t["state"] for t in get(url + f"{API}/state")["tickets"]}


def settled(url: str) -> bool:
    s = states(url)
    return bool(s) and all(v in ("DONE", "BLOCKED") for v in s.values())


def get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()


def browser_available() -> bool:
    """node, puppeteer-core (resolvable by node: NODE_PATH names its node_modules) and Chrome."""
    if not shutil.which("node") or not Path(CHROME).exists():
        return False
    return subprocess.run(["node", "-e", "require('puppeteer-core')"], capture_output=True, check=False).returncode == 0


DRIVE = """
const puppeteer = require("puppeteer-core");
const [url, chrome] = process.argv.slice(-2);          // node -e: the arguments come after the script
(async () => {
  const b = await puppeteer.launch({ executablePath: chrome, headless: "new", defaultViewport: { width: 1300, height: 800 } });
  const errors = [];
  try {
    const p = await b.newPage();
    p.on("pageerror", (e) => errors.push("page: " + e.message));
    p.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text() + " " + ((m.location() || {}).url || "")); });
    await p.goto(url + "/tickets", { waitUntil: "networkidle2" });
    await p.waitForFunction(() => document.getElementById("conn").textContent === "live", { timeout: 15000 }).catch(() => {});
    await new Promise((r) => setTimeout(r, 6000));                 // the banner polls every 5 s
    await p.click("#collapse"); await p.click("#frontier");         // show the done tickets too
    await new Promise((r) => setTimeout(r, 500));
    const got = await p.evaluate(() => ({ conn: document.getElementById("conn").textContent, nodes: document.getElementById("nodes").textContent,
                                          banner: (document.querySelector(".att-bar") || {}).textContent || "" }));
    console.log(JSON.stringify({ errors, ...got }));
  } finally { await b.close(); }
})();
"""


def drive_tickets_page(url: str) -> dict:
    p = subprocess.run(["node", "-e", DRIVE, url, CHROME], capture_output=True, text=True, timeout=90, check=False)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_tickets_are_worked_reviewed_and_done_or_blocked_by_programs(demo):
    url, target = demo
    ids = plan(target)
    assert ids == ["DEMO-1", "DEMO-2", "DEMO-3", "DEMO-4"]
    wait_until("the tickets", lambda: settled(url), timeout_s=120)
    assert states(url) == {"DEMO-1": "DONE", "DEMO-2": "DONE", "DEMO-3": "BLOCKED", "DEMO-4": "BLOCKED"}

    # the whole path of a done ticket, applied by the scheduler alone
    evs = get(url + "/api/events")["events"]                       # the last 500: the whole demo is fewer
    trail = [(e["data"]["from"], e["data"]["to"], e["source"]) for e in evs if e["type"] == "ticket.transitioned" and e.get("subject") == "DEMO-1"]
    assert [(a, b) for a, b, _ in trail] == [("OPEN", "IN_PROGRESS"), ("IN_PROGRESS", "READY_FOR_REVIEW"), ("READY_FOR_REVIEW", "IN_REVIEW"),
                                             ("IN_REVIEW", "APPROVED"), ("APPROVED", "DONE")]
    assert {src for _, _, src in trail} == {"task_scheduler"}
    protocol = [(e["type"], e["source"]) for e in evs if e.get("subject") == "DEMO-1" and e["type"] in ("task.assigned", "task.accepted", "task.reported")]
    assert protocol == [("task.assigned", "task_scheduler"), ("task.accepted", "worker/1"), ("task.reported", "worker/1"),
                        ("task.assigned", "task_scheduler"), ("task.accepted", "reviewer/1"), ("task.reported", "reviewer/1")]
    assert [e["source"] for e in evs if e["type"] == "task.requested"] == ["planner"] * 4

    # what the machines did, as the tickets record it
    one = get(url + f"{API}/tickets/DEMO-1")
    assert one["owner"] is None and [n["author"] for n in one["notes"]] == ["worker", "reviewer"]
    assert one["notes"][1]["text"].startswith("APPROVED")
    result = next((target.parent / "home").glob("*/tasks/workspaces/DEMO-1/RESULT.md"), None)
    assert result is not None and result.read_text().splitlines()[0] == "# DEMO-1: Say hello"
    four = get(url + f"{API}/tickets/DEMO-4")
    assert four["review_rounds"] == 1 and four["worker_turns"] == 2 and "yesterday.txt" in four["blocked_reason"]
    assert four["rejected_by"] == "reviewer" and any(n["kind"] == "review" and n["text"].startswith("REJECTED") for n in four["notes"])

    # blocked tickets wait for a planner: the attention endpoint every page reads
    attention = get(url + f"{API}/attention")
    assert attention["count"] == 2 and {b["id"] for b in attention["blocked"]} == {"DEMO-3", "DEMO-4"}
    assert "this ticket has none" in next(b["reason"] for b in attention["blocked"] if b["id"] == "DEMO-3")
    board = get(url + f"{API}/board")
    assert board["scheduler"] is True and board["counts"] == {"DONE": 2, "BLOCKED": 2}


@pytest.mark.skipif(not browser_available(), reason="no headless Chrome: needs node, puppeteer-core (NODE_PATH) and Chrome (CHROME)")
def test_the_tickets_page_draws_the_tickets_in_a_browser(demo):
    """The Tickets page, executed: it draws the four tickets and says it is live, without an error."""
    url, target = demo
    ids = plan(target)
    wait_until("the tickets", lambda: settled(url), timeout_s=120)
    page = drive_tickets_page(url)
    assert [e for e in page["errors"] if "/favicon.ico" not in e] == []     # Orchestra serves no favicon
    assert page["conn"] == "live"
    assert all(tid in page["nodes"] for tid in ids), page["nodes"]
    assert "2 tickets are blocked" in page["banner"]                  # attention.js, loaded by tasks-boot.js


def test_orchestra_observes_the_scheduler_and_the_machines(demo):
    url, _target = demo
    seen = {}

    def observed() -> bool:
        d = get(url + "/api/machines")
        if not d.get("ok"):
            return False
        seen.update({m["id"]: m for m in d["machines"]})
        return {"task_scheduler", "worker", "reviewer"} <= set(seen) and not any(seen[m].get("unseen") for m in ("worker", "reviewer"))
    wait_until("the machines", observed, timeout_s=60)
    assert seen["task_scheduler"]["package"] == "tasks" and seen["task_scheduler"]["mode"] == "continuous"
    assert "task.accepted" in seen["task_scheduler"]["subscribes"] and "task.requested" in seen["task_scheduler"]["subscribes"]
    for m in ("worker", "reviewer"):
        assert seen[m]["mode"] == "continuous" and seen[m]["about"]["kind"] == "program" and seen[m]["package"] is None
        assert [i["id"] for i in seen[m]["instances"]] == ["1"]
        assert "task.assigned" in seen[m]["subscribes"]
    assert get(url + "/api/plugins")["pages"] == [{"plugin": "tasks", "path": "/tickets", "title": "Tickets"}]
    html = get_text(url + "/tickets")
    assert "<title>Orchestra · Tickets</title>" in html and "/plugins/tasks/static/tasks.js" in html
    log = get(url + "/api/machines/task_scheduler/log")
    assert log["ok"] and any("project" in line for line in log["lines"])
