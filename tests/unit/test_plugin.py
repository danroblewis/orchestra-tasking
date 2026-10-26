"""The task system appears in Orchestra's web app through its entry point (pyproject.toml)."""

import subprocess

import pytest
from fastapi.testclient import TestClient
from orchestra.config import load_project as load_orchestra_project
from orchestra.web.app import create_app

from tasking.config import init_project


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return init_project(repo, prefix="T")


def test_orchestra_serves_the_task_pages_and_api(project):
    c = TestClient(create_app(load_orchestra_project(project.repo_root)))
    d = c.get("/api/plugins").json()
    assert [p["path"] for p in d["pages"]] == ["/tickets", "/agents"] and d["errors"] == []
    assert "tasks.js" in c.get("/tickets").text and "tasks.js" in c.get("/agents").text
    assert c.get("/plugins/tasks/static/tasks.js").status_code == 200
    assert c.get("/api/plugins/tasks/state").json()["tickets"] == []
    assert {a["id"] for a in c.get("/api/plugins/tasks/agents").json()["agents"]} >= {"supervisor", "coordinator", "worker", "reviewer"}
