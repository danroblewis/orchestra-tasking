"""``tasking init`` writes the repository's own base branch, not a fixed ``main``."""

import subprocess

import pytest

from tasking.config import init_project


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
    r = tmp_path / "repo"
    r.mkdir()
    git("init", "-q", "-b", "master", cwd=r)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=r)
    return r


def test_init_writes_the_branch_checked_out(repo):
    assert init_project(repo, prefix="T").config.base_branch == "master"


def test_init_prefers_the_remotes_default_branch(repo):
    git("switch", "-q", "-c", "feature", cwd=repo)
    git("update-ref", "refs/remotes/origin/trunk", "HEAD", cwd=repo)
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk", cwd=repo)
    assert init_project(repo, prefix="T").config.base_branch == "trunk"


def test_init_takes_the_base_branch_given(repo):
    assert init_project(repo, prefix="T", base_branch="develop").config.base_branch == "develop"


def test_init_falls_back_to_main_on_a_detached_head(repo):
    git("checkout", "-q", "--detach", cwd=repo)
    assert init_project(repo, prefix="T").config.base_branch == "main"
