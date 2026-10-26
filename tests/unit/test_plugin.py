"""The task system appears in Orchestra's web app through its entry point (pyproject.toml)."""

from fastapi.testclient import TestClient
from orchestra.config import load_project as load_orchestra_project
from orchestra.web.app import create_app


def test_orchestra_serves_the_task_pages_and_api(project):
    c = TestClient(create_app(load_orchestra_project(project.repo_root)))
    d = c.get("/api/plugins").json()
    assert [p["path"] for p in d["pages"] if p["plugin"] == "tasks"] == ["/tickets"] and d["errors"] == []
    assert d["scripts"] == ["/plugins/tasks/static/tasks-boot.js"]            # one loader; it loads the rest after tasks.js
    assert "tasks.js" in c.get("/tickets").text
    for f in ("tasks-boot.js", "tasks.js", "attention.js", "machine-cards.js", "tasks-panel.js", "tasks.css"):
        assert c.get(f"/plugins/tasks/static/{f}").status_code == 200
    state = c.get("/api/plugins/tasks/state").json()
    assert set(state) == {"tickets", "milestones", "counts", "now", "scheduler"} and state["tickets"] == []
    assert c.get("/api/plugins/tasks/board").json()["tickets"] == []
    assert c.get("/api/plugins/tasks/attention").json() == {"ok": True, "blocked": [], "count": 0}
    machines = {m["id"]: m for m in c.get("/api/machines").json()["machines"]}
    assert machines["task_scheduler"]["package"] == "tasks" and machines["task_web"]["package"] == "tasks"
