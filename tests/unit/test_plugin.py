"""The task system appears in Orchestra's web app through its entry point (pyproject.toml), and asks
`orchestra run` to keep its scheduler running."""

import subprocess

from fastapi.testclient import TestClient
from orchestra.config import load_project as load_orchestra_project
from orchestra.web.app import create_app


def test_orchestra_serves_the_task_pages_and_api(project):
    c = TestClient(create_app(load_orchestra_project(project.repo_root)))
    d = c.get("/api/plugins").json()
    assert [p["path"] for p in d["pages"] if p["plugin"] == "tasks"] == ["/tickets"] and d["errors"] == []
    assert d["scripts"] == ["/plugins/tasks/static/tasks-boot.js"]            # one loader; it loads the rest after tasks.js
    assert "tasks.js" in c.get("/tickets").text
    assert '<link rel="stylesheet" href="/plugins/tasks/static/tasks.css">' in c.get("/").text   # the look, on every page
    for f in ("tasks-boot.js", "tasks.js", "attention.js", "machine-cards.js", "tasks-panel.js", "tasks.css"):
        assert c.get(f"/plugins/tasks/static/{f}").status_code == 200
    state = c.get("/api/plugins/tasks/state").json()
    assert set(state) == {"tickets", "milestones", "counts", "scheduler"} and state["tickets"] == []
    assert c.get("/api/plugins/tasks/board").json()["tickets"] == []
    assert c.get("/api/plugins/tasks/attention").json() == {"ok": True, "blocked": [], "count": 0}
    machines = {m["id"]: m for m in c.get("/api/machines").json()["machines"]}
    assert machines["task_scheduler"]["package"] == "tasks" and "task_web" not in machines     # declared: the scheduler only


def test_the_capacity_ceiling_follows_orchestra_toml(project):
    """Every request re-reads the task system's config, so an edit to [tasks] is live on the Dashboard."""
    c = TestClient(create_app(load_orchestra_project(project.repo_root)))
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
