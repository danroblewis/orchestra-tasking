"""The task system appears in Orchestra's web app through its entry point (pyproject.toml), and asks
`orchestra run` to keep its scheduler running."""

import subprocess

from fastapi.testclient import TestClient
from orchestra.config import load_project as load_orchestra_project
from orchestra.web.app import create_app
from orchestra.web.auth import bearer


def test_orchestra_serves_the_task_pages_and_api(project):
    o = load_orchestra_project(project.repo_root)
    c = TestClient(create_app(o), headers=bearer(o))
    d = c.get("/api/plugins").json()
    assert [p["path"] for p in d["pages"] if p["plugin"] == "tasks"] == ["/tickets"] and d["errors"] == []
    assert d["scripts"] == ["/plugins/tasks/static/tasks-boot.js"]            # one loader; it loads the rest after tasks.js
    assert "tasks.js" in c.get("/tickets").text
    assert '<link rel="stylesheet" href="/plugins/tasks/static/tasks.css">' in c.get("/").text   # the look, on every page
    for f in ("tasks-boot.js", "tasks.js", "attention.js", "tasks-panel.js", "tasks.css"):
        assert c.get(f"/plugins/tasks/static/{f}").status_code == 200
    state = c.get("/api/plugins/tasks/state").json()
    assert set(state) == {"tickets", "milestones", "counts", "scheduler"} and state["tickets"] == []
    assert c.get("/api/plugins/tasks/board").json()["tickets"] == []
    assert c.get("/api/plugins/tasks/attention").json() == {"ok": True, "blocked": [], "count": 0}
    machines = {m["id"]: m for m in c.get("/api/machines").json()["machines"]}
    assert "task_scheduler" not in machines and "task_web" not in machines     # the scheduler is no machine: nothing declared
    assert "tasks_tickets{" in c.get("/api/plugins/tasks/metrics").text       # the measurements, as the instrument reads them


def test_the_capacity_ceiling_follows_orchestra_toml(project):
    """Every request re-reads the task system's config, so an edit to [tasks] is live on the Dashboard."""
    o = load_orchestra_project(project.repo_root)
    c = TestClient(create_app(o), headers=bearer(o))
    assert c.get("/api/plugins/tasks/board").json()["capacity"]["max_workers"] == 2
    toml = project.repo_root / "orchestra.toml"
    toml.write_text(toml.read_text().replace("workers = 2", "workers = 5"))
    assert c.get("/api/plugins/tasks/board").json()["capacity"]["max_workers"] == 5


def test_orchestra_run_is_asked_to_keep_the_scheduler_running_in_a_project_set_up_for_tasks(project, tmp_path):
    import sys

    from tasking.web import plugin

    p = plugin(load_orchestra_project(project.repo_root))
    assert [(x.name, x.argv(), x.restart) for x in p.processes] == [("scheduler", [sys.executable, "-m", "tasking", "run", "--wait"], "always")]
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=bare, check=True)
    (bare / "orchestra.toml").write_text('[events]\nurl = "file"\n')
    assert plugin(load_orchestra_project(bare)).processes == []        # not set up for tasks: no scheduler


def test_the_operators_controls_need_a_login_and_orchestras_own_pages(project):
    """POST /control as the Tasks panel sends it: the session cookie and the page's origin. Without a
    login, or from another site, Orchestra refuses it before the plugin sees it."""
    from orchestra.web.auth import token

    o = load_orchestra_project(project.repo_root)
    browser = TestClient(create_app(o))
    page = {"Origin": "http://testserver"}
    assert browser.post("/api/plugins/tasks/control", json={"action": "pause"}, headers=page).status_code == 401
    assert browser.post("/login", data={"token": token(o), "next": "/"}, headers=page, follow_redirects=False).status_code == 303
    assert browser.post("/api/plugins/tasks/control", json={"action": "pause"}).status_code == 403
    assert browser.post("/api/plugins/tasks/control", json={"action": "pause"},
                        headers={"Origin": "https://elsewhere.example"}).status_code == 403
    assert browser.get("/api/plugins/tasks/board").json()["pickup_paused"] is False
    assert browser.post("/api/plugins/tasks/control", json={"action": "pause"}, headers=page).json()["ok"]
    assert browser.get("/api/plugins/tasks/board").json()["pickup_paused"] is True
